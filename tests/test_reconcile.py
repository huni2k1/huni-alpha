"""Reconcile pathway: record PnL from Binance fills when a position is closed externally."""

import copy

import pytest

from trading_bot import trader


def _fresh_state():
    # Deepcopy guards against list/dict aliasing, and explicit reset guards against
    # other test modules that shallow-copy _EMPTY_STATE and leave residue in its nested dicts.
    s = copy.deepcopy(trader._EMPTY_STATE)
    s["positions"] = {}
    s["trade_log"] = []
    s["last_signal"] = {}
    return s


class _FakeClient:
    """Minimal Binance client stub for reconcile tests."""

    def __init__(
        self,
        live_positions=None,
        user_trades=None,
        order_statuses=None,
    ):
        self._live_positions = live_positions or []
        self._user_trades = user_trades or []
        self._order_statuses = order_statuses or {}
        self.cancel_calls = []

    def get_open_positions(self):
        return list(self._live_positions)

    def get_user_trades(self, symbol, start_ms=None, limit=100):
        return [t for t in self._user_trades if t["symbol"] == symbol]

    def get_order_status(self, symbol, order_id):
        return self._order_statuses.get(order_id)

    def cancel_all_orders(self, symbol):
        self.cancel_calls.append(symbol)
        return True


def _base_position(**overrides):
    pos = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "entry_price": 100.0,
        "entry_time": "2026-04-17T10:00:00+00:00",
        "quantity": 1.0,
        "position_size_usdt": 100.0,
        "tp_order_id": "algo:tp-1",
        "sl_order_id": "algo:sl-1",
        "score": 7.5,
        "strategy": "test",
        "protection_status": "armed",
    }
    pos.update(overrides)
    return pos


def test_record_reconciled_close_records_tp_hit(monkeypatch):
    sent = []
    monkeypatch.setattr(trader, "send_telegram", lambda msg: sent.append(msg))

    state = _fresh_state()
    state["positions"]["BTCUSDT"] = _base_position()
    client = _FakeClient(
        user_trades=[
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "qty": "1.0",
                "price": "105.0",
                "realizedPnl": "5.0",
                "time": 1713355200000,
            }
        ],
        order_statuses={"algo:tp-1": "FILLED", "algo:sl-1": "EXPIRED"},
    )

    trader._record_reconciled_close(state, client, "BTCUSDT")

    assert len(state["trade_log"]) == 1
    trade = state["trade_log"][0]
    assert trade["symbol"] == "BTCUSDT"
    assert trade["direction"] == "LONG"
    assert trade["exit_price"] == 105.0
    assert trade["exit_reason"] == "TP"
    assert trade["pnl_usd"] == 5.0
    assert trade["pnl_pct"] == 5.0
    assert trade["reconciled"] is True
    assert sent and "TP" in sent[0] and "reconciled" in sent[0]


def test_record_reconciled_close_records_sl_hit_short(monkeypatch):
    sent = []
    monkeypatch.setattr(trader, "send_telegram", lambda msg: sent.append(msg))

    state = _fresh_state()
    state["positions"]["ETHUSDT"] = _base_position(
        symbol="ETHUSDT", direction="SHORT", entry_price=200.0, position_size_usdt=200.0
    )
    client = _FakeClient(
        user_trades=[
            {
                "symbol": "ETHUSDT",
                "side": "BUY",
                "qty": "1.0",
                "price": "210.0",
                "realizedPnl": "-10.0",
                "time": 1713355200000,
            }
        ],
        order_statuses={"algo:tp-1": "EXPIRED", "algo:sl-1": "FILLED"},
    )

    trader._record_reconciled_close(state, client, "ETHUSDT")

    trade = state["trade_log"][0]
    assert trade["direction"] == "SHORT"
    assert trade["exit_reason"] == "SL"
    assert trade["pnl_usd"] == -10.0
    assert trade["pnl_pct"] == -5.0


def test_record_reconciled_close_weighted_exit_price_multiple_fills(monkeypatch):
    monkeypatch.setattr(trader, "send_telegram", lambda msg: None)

    state = _fresh_state()
    state["positions"]["BTCUSDT"] = _base_position(quantity=2.0, position_size_usdt=200.0)
    client = _FakeClient(
        user_trades=[
            {"symbol": "BTCUSDT", "side": "SELL", "qty": "1.0", "price": "110.0", "realizedPnl": "10.0", "time": 1713355200000},
            {"symbol": "BTCUSDT", "side": "SELL", "qty": "1.0", "price": "120.0", "realizedPnl": "20.0", "time": 1713355300000},
        ],
        order_statuses={"algo:tp-1": "FILLED"},
    )

    trader._record_reconciled_close(state, client, "BTCUSDT")

    trade = state["trade_log"][0]
    assert trade["exit_price"] == 115.0  # (1*110 + 1*120) / 2
    assert trade["pnl_usd"] == 30.0


def test_record_reconciled_close_no_fills_sends_warning_no_trade_record(monkeypatch):
    sent = []
    monkeypatch.setattr(trader, "send_telegram", lambda msg: sent.append(msg))

    state = _fresh_state()
    state["positions"]["BTCUSDT"] = _base_position()
    client = _FakeClient(user_trades=[], order_statuses={})

    trader._record_reconciled_close(state, client, "BTCUSDT")

    assert state["trade_log"] == []
    assert sent and "Silent close" in sent[0]


def test_reconcile_with_binance_removes_stale_and_records_pnl(monkeypatch):
    monkeypatch.setattr(trader, "send_telegram", lambda msg: None)
    monkeypatch.setattr(trader, "save_state", lambda s: None)

    state = _fresh_state()
    state["positions"]["BTCUSDT"] = _base_position()
    client = _FakeClient(
        live_positions=[],
        user_trades=[
            {"symbol": "BTCUSDT", "side": "SELL", "qty": "1.0", "price": "105.0", "realizedPnl": "5.0", "time": 1713355200000}
        ],
        order_statuses={"algo:tp-1": "FILLED"},
    )

    trader.reconcile_with_binance(state, client)

    assert "BTCUSDT" not in state["positions"]
    assert len(state["trade_log"]) == 1
    assert state["trade_log"][0]["exit_reason"] == "TP"
    assert client.cancel_calls == ["BTCUSDT"]


# ── Algo status normalization ──────────────────────────────────────


def _make_stubbed_client(response):
    """Build a BinanceClient whose _request returns the given payload."""
    from trading_bot.binance_client import BinanceClient

    client = BinanceClient.__new__(BinanceClient)
    client._request = lambda *args, **kwargs: response
    return client


def test_algo_status_completed_normalized_to_filled():
    client = _make_stubbed_client({"algoStatus": "COMPLETED"})
    assert client.get_order_status("BTCUSDT", "algo:x") == "FILLED"


def test_algo_status_cancelled_normalized_to_canceled():
    client = _make_stubbed_client({"algoStatus": "CANCELLED"})
    assert client.get_order_status("BTCUSDT", "algo:x") == "CANCELED"


def test_regular_order_status_passthrough():
    client = _make_stubbed_client({"status": "FILLED"})
    assert client.get_order_status("BTCUSDT", "12345") == "FILLED"


def test_algo_status_executing_passthrough():
    client = _make_stubbed_client({"algoStatus": "EXECUTING"})
    assert client.get_order_status("BTCUSDT", "algo:x") == "EXECUTING"
