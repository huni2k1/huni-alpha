import pytest

from trading_bot import binance_client as bc


class _FakeResponse:
    def __init__(self, ok=True, status_code=200, json_data=None):
        self.ok = ok
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}

    def json(self):
        return self._json_data


def _make_client():
    client = bc.BinanceClient.__new__(bc.BinanceClient)
    client._api_key = "test-key"
    client._api_secret = b"test-secret"
    client.base_urls = ["https://primary", "https://secondary"]
    client.base_url = client.base_urls[0]
    client._request_retries = 1
    client._request_backoff_sec = 0.0
    client._symbol_cache = {}
    return client


def test_constructor_uses_constructor_args_and_live_mode(monkeypatch):
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)

    client = bc.BinanceClient(api_key="k", api_secret="s", testnet=False)

    assert client.testnet is False
    assert client.base_url == bc.BASE_URL_LIVE
    assert client.base_urls == list(bc.BASE_URL_LIVE_FALLBACKS)


def test_constructor_uses_env_credentials_and_defaults_to_testnet(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "env-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "env-secret")
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)

    client = bc.BinanceClient()

    assert client._api_key == "env-key"
    assert client._api_secret == b"env-secret"
    assert client.testnet is True
    assert client.base_urls == [bc.BASE_URL_TESTNET]


def test_constructor_reads_credentials_from_config(monkeypatch, tmp_path):
    cfg = tmp_path / "binance-trading.json"
    cfg.write_text('{"api_key": "cfg-key", "api_secret": "cfg-secret"}')
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.setattr(bc, "_CONFIG_PATH", str(cfg))

    client = bc.BinanceClient()

    assert client._api_key == "cfg-key"
    assert client._api_secret == b"cfg-secret"


def test_constructor_raises_when_credentials_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.setattr(bc, "_CONFIG_PATH", str(tmp_path / "missing.json"))

    with pytest.raises(ValueError):
        bc.BinanceClient()


def test_sign_appends_timestamp_and_signature(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(bc.time, "time", lambda: 1234.567)

    query = client._sign({"symbol": "BTCUSDT", "recvWindow": 5000})

    assert "symbol=BTCUSDT" in query
    assert "recvWindow=5000" in query
    assert "timestamp=1234567" in query
    assert "signature=" in query


def test_request_uses_failover_host_after_primary_transient_error(monkeypatch):
    client = _make_client()
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers, timeout))
        if "primary" in url:
            return _FakeResponse(ok=False, status_code=503, json_data={"code": -1001, "msg": "busy"})
        return _FakeResponse(ok=True, json_data={"markPrice": "123.45"})

    monkeypatch.setattr(bc.requests, "get", fake_get)

    data = client._request("GET", "/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"}, signed=False)

    assert data == {"markPrice": "123.45"}
    assert len(calls) == 2
    assert calls[0][0] == "https://primary/fapi/v1/premiumIndex"
    assert calls[1][0] == "https://secondary/fapi/v1/premiumIndex"
    assert calls[0][1] == {"symbol": "BTCUSDT"}
    assert calls[1][1] == {"symbol": "BTCUSDT"}


def test_request_returns_none_on_non_transient_http_error(monkeypatch):
    client = _make_client()

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResponse(ok=False, status_code=401, json_data={"code": -2015, "msg": "Invalid key"})

    monkeypatch.setattr(bc.requests, "get", fake_get)

    assert client._request("GET", "/fapi/v2/balance", signed=True) is None


def test_request_handles_non_json_error_response(monkeypatch):
    client = _make_client()

    class _BadJsonResponse(_FakeResponse):
        def json(self):
            raise ValueError("bad json")

    monkeypatch.setattr(bc.requests, "get", lambda *args, **kwargs: _BadJsonResponse(ok=False, status_code=400))

    assert client._request("GET", "/fapi/v2/balance", signed=True) is None


def test_request_returns_none_after_network_errors(monkeypatch):
    client = _make_client()
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        raise bc.RequestException("DNS failure")

    monkeypatch.setattr(bc.requests, "get", fake_get)

    result = client._request("GET", "/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"})

    assert result is None
    assert len(calls) == 4  # 2 hosts * (1 retry + 1 retry attempt)


def test_request_signed_puts_signature_in_url(monkeypatch):
    client = _make_client()
    captured = {}

    def fake_post(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _FakeResponse(ok=True, json_data={"orderId": 123})

    monkeypatch.setattr(client, "_sign", lambda params: "symbol=BTCUSDT&timestamp=1&signature=abc")
    monkeypatch.setattr(bc.requests, "post", fake_post)

    result = client._request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"}, signed=True)

    assert result == {"orderId": 123}
    assert captured["url"] == "https://primary/fapi/v1/order?symbol=BTCUSDT&timestamp=1&signature=abc"
    assert captured["params"] is None
    assert captured["headers"]["X-MBX-APIKEY"] == "test-key"


def test_get_symbol_info_extracts_and_caches_filters(monkeypatch):
    client = _make_client()
    requests = []

    def fake_request(method, endpoint, params=None, signed=False):
        requests.append((method, endpoint, params, signed))
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "quantityPrecision": 3,
                    "pricePrecision": 2,
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": "0.005"},
                        {"filterType": "MIN_NOTIONAL", "notional": "100"},
                    ],
                }
            ]
        }

    monkeypatch.setattr(client, "_request", fake_request)

    info1 = client.get_symbol_info("BTCUSDT")
    info2 = client.get_symbol_info("BTCUSDT")

    assert info1 == {
        "qty_precision": 3,
        "price_precision": 2,
        "min_qty": 0.005,
        "min_notional": 100.0,
    }
    assert info2 == info1
    assert requests == [("GET", "/fapi/v1/exchangeInfo", None, False)]


def test_get_symbol_info_returns_none_when_symbol_missing(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {"symbols": [{"symbol": "ETHUSDT", "filters": []}]})

    assert client.get_symbol_info("BTCUSDT") is None


def test_get_usdt_balance_returns_matching_asset(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: [
        {"asset": "BNB", "availableBalance": "1.0"},
        {"asset": "USDT", "availableBalance": "12.34"},
    ])

    assert client.get_usdt_balance() == 12.34


def test_get_usdt_balance_returns_none_without_data(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: None)

    assert client.get_usdt_balance() is None


def test_get_usdt_equity_adds_wallet_and_unrealized(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: {"totalWalletBalance": "50.5", "totalUnrealizedProfit": "-0.5"},
    )

    assert client.get_usdt_equity() == 50.0


def test_get_open_positions_filters_zero_size(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: [{"positionAmt": "0"}, {"positionAmt": "2.5"}, {"positionAmt": "-1"}],
    )

    positions = client.get_open_positions()
    assert len(positions) == 2


def test_get_open_algo_orders_supports_dict_and_list_shapes(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {"orders": [{"id": 1}]})
    assert client.get_open_algo_orders("BTCUSDT") == [{"id": 1}]
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: [{"id": 2}])
    assert client.get_open_algo_orders("BTCUSDT") == [{"id": 2}]


def test_get_mark_price_returns_float(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {"markPrice": "987.65"})

    assert client.get_mark_price("BTCUSDT") == 987.65


def test_ensure_one_way_mode_returns_true_when_already_one_way(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {"dualSidePosition": False})

    assert client.ensure_one_way_mode() is True


def test_ensure_one_way_mode_switches_from_hedge(monkeypatch):
    client = _make_client()
    calls = []

    def fake_request(method, endpoint, params=None, signed=False):
        calls.append((method, endpoint, params, signed))
        if endpoint == "/fapi/v1/positionSide/dual" and method == "GET":
            return {"dualSidePosition": True}
        return {"ok": True}

    monkeypatch.setattr(client, "_request", fake_request)

    assert client.ensure_one_way_mode() is True
    assert calls[1] == ("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"}, True)


def test_set_leverage_returns_true_on_success(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {"leverage": 3})

    assert client.set_leverage("BTCUSDT", leverage=3) is True


def test_place_market_order_maps_response(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: {"orderId": 1, "avgPrice": "100.5", "executedQty": "0.2", "status": "FILLED"},
    )

    assert client.place_market_order("BTCUSDT", "BUY", 0.2) == {
        "order_id": "1",
        "filled_price": 100.5,
        "filled_qty": 0.2,
        "status": "FILLED",
    }


def test_place_market_close_returns_none_on_failure(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: None)

    assert client.place_market_close("BTCUSDT", "SELL", 0.2) is None


def test_cancel_order_uses_algo_endpoint_for_algo_refs(monkeypatch):
    client = _make_client()
    calls = []

    def fake_request(method, endpoint, params=None, signed=False):
        calls.append((method, endpoint, params, signed))
        return {"ok": True}

    monkeypatch.setattr(client, "_request", fake_request)

    assert client.cancel_order("BTCUSDT", "algo:tp_123") is True
    assert calls == [
        ("DELETE", "/fapi/v1/algoOrder", {"symbol": "BTCUSDT", "clientAlgoId": "tp_123"}, True)
    ]


def test_cancel_order_uses_regular_endpoint_for_regular_refs(monkeypatch):
    client = _make_client()
    calls = []
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True})

    assert client.cancel_order("BTCUSDT", "12345") is True
    assert calls[0] == (
        ("DELETE", "/fapi/v1/order", {"symbol": "BTCUSDT", "orderId": "12345"}),
        {"signed": True},
    )


def test_cancel_all_orders_requires_both_calls_to_succeed(monkeypatch):
    client = _make_client()
    results = iter([{"ok": True}, None])
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: next(results))

    assert client.cancel_all_orders("BTCUSDT") is False


def test_place_tp_order_wraps_client_algo_id(monkeypatch):
    client = _make_client()

    monkeypatch.setattr(bc, "uuid4", lambda: type("U", (), {"hex": "1234567890abcdef1234567890abcdef"})())
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: {"clientAlgoId": "tp_BTCUSDT_custom"},
    )

    order_id = client.place_tp_order("BTCUSDT", "SELL", 123.456, 2)

    assert order_id == "algo:tp_BTCUSDT_custom"


def test_place_sl_order_returns_none_on_failure(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(bc, "uuid4", lambda: type("U", (), {"hex": "abcdef1234567890abcdef1234567890"})())
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: None)

    assert client.place_sl_order("BTCUSDT", "BUY", 99.123, 2) is None


def test_get_order_status_returns_none_without_result():
    client = _make_client()
    client._request = lambda *args, **kwargs: None

    assert client.get_order_status("BTCUSDT", "123") is None


def test_get_order_fill_price_prefers_algo_fields():
    client = _make_client()
    client._request = lambda *args, **kwargs: {"avgExecutedPrice": "101.25"}

    assert client.get_order_fill_price("BTCUSDT", "algo:tp_1") == 101.25


def test_get_order_fill_price_returns_none_when_zero():
    client = _make_client()
    client._request = lambda *args, **kwargs: {"avgPrice": "0"}

    assert client.get_order_fill_price("BTCUSDT", "123") is None


def test_get_user_trades_returns_empty_for_non_list_payload():
    client = _make_client()
    client._request = lambda *args, **kwargs: {"unexpected": "shape"}

    assert client.get_user_trades("BTCUSDT") == []


def test_get_user_trades_passes_start_time(monkeypatch):
    client = _make_client()
    seen = {}

    def fake_request(method, endpoint, params=None, signed=False):
        seen["params"] = params
        return []

    monkeypatch.setattr(client, "_request", fake_request)

    client.get_user_trades("BTCUSDT", start_ms=123456789, limit=50)

    assert seen["params"] == {"symbol": "BTCUSDT", "limit": 50, "startTime": 123456789}
