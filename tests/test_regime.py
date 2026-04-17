"""BTC regime classifier + scanner scope filtering."""

from trading_bot import regime, scanner


def _make_candles(closes: list[float], start_ms: int = 1_700_000_000_000) -> list[dict]:
    return [
        {"close": c, "close_time": start_ms + i * 3_600_000}
        for i, c in enumerate(closes)
    ]


def test_classify_regime_series_warmup_is_unknown():
    # Not enough data for EMA(200) → everything labeled unknown
    closes = [100.0] * 100
    labels = regime.classify_regime_series(_make_candles(closes))
    assert all(label == "unknown" for label in labels)


def test_classify_regime_bull_uptrend():
    # Monotonic uptrend for 300 candles → last candle should be "bull"
    closes = [100.0 + i * 0.5 for i in range(300)]
    labels = regime.classify_regime_series(_make_candles(closes))
    assert labels[-1] == "bull"


def test_classify_regime_bear_downtrend():
    closes = [200.0 - i * 0.5 for i in range(300)]
    labels = regime.classify_regime_series(_make_candles(closes))
    assert labels[-1] == "bear"


def test_classify_regime_chop_flat():
    # Perfectly flat prices → EMA slope zero → labeled chop regardless of close vs EMA.
    closes = [100.0] * 300
    labels = regime.classify_regime_series(_make_candles(closes))
    assert labels[-1] == "chop"


def test_classify_current_regime_empty():
    assert regime.classify_current_regime([]) == "unknown"


def test_build_regime_lookup_indexes_by_close_time():
    closes = [100.0 + i * 0.5 for i in range(300)]
    candles = _make_candles(closes)
    lookup = regime.build_regime_lookup(candles)
    assert len(lookup) == len(candles)
    # Last candle should be bull
    assert lookup[candles[-1]["close_time"]] == "bull"


# ── Scanner scope filter ──────────────────────────────────────────


def test_setup_matches_scope_regime_gate_pass():
    setup = {"scope_regime": "bull", "scope_symbol": None}
    assert scanner._setup_matches_scope(setup, "BTCUSDT", current_regime="bull")


def test_setup_matches_scope_regime_gate_reject():
    setup = {"scope_regime": "bull", "scope_symbol": None}
    assert not scanner._setup_matches_scope(setup, "BTCUSDT", current_regime="bear")


def test_setup_matches_scope_regime_null_matches_any():
    setup = {"scope_regime": None, "scope_symbol": None}
    assert scanner._setup_matches_scope(setup, "BTCUSDT", current_regime="bull")
    assert scanner._setup_matches_scope(setup, "BTCUSDT", current_regime="bear")
    assert scanner._setup_matches_scope(setup, "BTCUSDT", current_regime=None)


def test_setup_matches_scope_current_regime_none_bypasses_filter():
    """Back-compat: when caller doesn't classify regime, don't gate."""
    setup = {"scope_regime": "bull", "scope_symbol": None}
    assert scanner._setup_matches_scope(setup, "BTCUSDT", current_regime=None)


def test_setup_matches_scope_symbol_and_regime_combined():
    setup = {"scope_regime": "bear", "scope_symbol": "ETHUSDT"}
    assert scanner._setup_matches_scope(setup, "ETHUSDT", current_regime="bear")
    assert not scanner._setup_matches_scope(setup, "BTCUSDT", current_regime="bear")  # wrong symbol
    assert not scanner._setup_matches_scope(setup, "ETHUSDT", current_regime="bull")  # wrong regime


def test_setup_scope_specificity_ranking():
    symbol_setup = {"scope_type": "symbol"}
    regime_setup = {"scope_type": "regime"}
    pooled_setup = {"scope_type": "pooled"}
    # symbol > regime > pooled so most specific match wins sort
    assert scanner._setup_scope_specificity(symbol_setup) > scanner._setup_scope_specificity(regime_setup)
    assert scanner._setup_scope_specificity(regime_setup) > scanner._setup_scope_specificity(pooled_setup)


# ── Analyzer row tagging ─────────────────────────────────────────


def test_tag_rows_with_regime_populates_field():
    from trading_bot import setup_analyzer

    btc_candles = _make_candles([100.0 + i * 0.5 for i in range(300)])
    rows = [
        {"symbol": "ETHUSDT", "close_time": btc_candles[-1]["close_time"]},
        {"symbol": "SOLUSDT", "close_time": btc_candles[-2]["close_time"]},
        {"symbol": "BNBUSDT", "close_time": 999_999_999_999},  # outside BTC series
    ]
    setup_analyzer.tag_rows_with_regime(rows, btc_candles)

    assert rows[0]["regime"] == "bull"
    assert rows[1]["regime"] == "bull"
    assert rows[2]["regime"] == "unknown"


def test_tag_rows_with_regime_no_btc_candles_graceful():
    from trading_bot import setup_analyzer

    rows = [{"symbol": "ETHUSDT", "close_time": 1}]
    setup_analyzer.tag_rows_with_regime(rows, [])
    assert rows[0]["regime"] == "unknown"


def test_build_analysis_views_splits_by_regime():
    from trading_bot import setup_analyzer

    rows = [
        {"symbol": "BTCUSDT", "regime": "bull"},
        {"symbol": "BTCUSDT", "regime": "bull"},
        {"symbol": "ETHUSDT", "regime": "bear"},
        {"symbol": "ETHUSDT", "regime": "chop"},
        {"symbol": "ETHUSDT", "regime": "unknown"},
    ]
    views = setup_analyzer.build_analysis_views(rows, ["BTCUSDT", "ETHUSDT"], discovery_variants=["regime"])

    assert set(views) == {"regime_bull", "regime_bear", "regime_chop"}
    assert len(views["regime_bull"]["rows"]) == 2
    assert len(views["regime_bear"]["rows"]) == 1
    assert len(views["regime_chop"]["rows"]) == 1
    # "unknown" rows must never leak into a regime view
    for view in views.values():
        assert all(row["regime"] != "unknown" for row in view["rows"])
    # Scope context carries scope_regime so validated setups are tagged downstream
    assert views["regime_bull"]["scope"]["scope_regime"] == "bull"
    assert views["regime_bull"]["scope"]["scope_type"] == "regime"
