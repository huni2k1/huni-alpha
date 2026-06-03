from trading_bot import setup_analyzer as analyzer


def make_candle(open_price, high, low, close, hour_index):
    open_time_ms = 1_700_000_000_000 + hour_index * 3_600_000
    return {
        "open_time": open_time_ms,
        "close_time": open_time_ms + 3_600_000 - 1,
        "open": float(open_price),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": 1000.0,
    }


def test_simulate_forward_trade_hits_tp_for_long():
    candles = [
        make_candle(100, 101, 99, 100, 0),
        make_candle(100, 101, 99, 100, 1),
        make_candle(100, 101, 99, 100, 2),
        make_candle(100, 101, 99, 100, 3),
        make_candle(100, 101, 99, 100, 4),
        make_candle(100, 101, 99, 100, 5),
        make_candle(100, 101, 99, 100, 6),
        make_candle(100, 101, 99, 100, 7),
        make_candle(100, 101, 99, 100, 8),
        make_candle(100, 101, 99, 100, 9),
        make_candle(100, 101, 99, 100, 10),
        make_candle(100, 101, 99, 100, 11),
        make_candle(100, 101, 99, 100, 12),
        make_candle(100, 101, 99, 100, 13),
        make_candle(100, 101, 99, 100, 14),
        make_candle(100, 101, 99, 100, 15),
        make_candle(100, 101, 99, 100, 16),
        make_candle(100, 101, 99, 100, 17),
        make_candle(100, 101, 99, 100, 18),
        make_candle(100, 101, 99, 100, 19),
        make_candle(100, 104, 99.5, 103, 20),
    ]
    outcome = analyzer.simulate_forward_trade(
        candles,
        signal_idx=19,
        direction="LONG",
        sl_atr_mult=1.0,
        rr_ratio=2.0,
        fee_pct=0.0,
        slippage_pct=0.0,
    )
    assert outcome is not None
    assert outcome["exit_reason"] == "TP"
    assert outcome["is_win"] is True


def test_compute_indicator_snapshot_includes_new_price_action_and_structure_fields():
    candles = [
        make_candle(100, 101, 99, 99, 0),
        make_candle(98, 105, 97, 104, 1),
        make_candle(104, 104.5, 103.8, 104.2, 2),
        make_candle(104.2, 104.5, 101.0, 104.3, 3),
    ]
    context = analyzer.precompute_symbol_context(candles)
    snapshot = analyzer.compute_indicator_snapshot("BTCUSDT", candles, 1, context=context)

    assert snapshot["bullish_engulfing"] is True
    assert snapshot["inside_bar"] is False
    assert snapshot["three_green_candles"] is False
    assert snapshot["near_20bar_high"] is True
    assert "atr_percentile_high" in snapshot
    assert "candle_range_above_atr" in snapshot
    assert "rsi_bullish_divergence" in snapshot
    assert "macd_bullish_divergence" in snapshot
    assert "breaks_20bar_high" in snapshot
    assert "strong_ema21_slope_up" in snapshot
    assert "two_expansion_green_candles" in snapshot
    assert "htf_4h_bull_trend" in snapshot


def test_build_4h_context_groups_by_utc_boundary():
    # 12 candles starting at 2024-01-01 00:00 UTC (hourly)
    base_ms = 1704067200000  # 2024-01-01 00:00 UTC
    candles = []
    for h in range(12):
        open_time = base_ms + h * 3_600_000
        candles.append({
            "open_time": open_time,
            "close_time": open_time + 3_599_999,
            "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.0 + h * 0.01,
            "volume": 1000.0,
        })

    ctx = analyzer._build_4h_context(candles)
    count = ctx["count_by_idx"]

    # Hours 0-3 belong to 00:00 boundary — no completed bar yet
    assert count[0] == count[1] == count[2] == count[3] == 0
    # Hours 4-7 belong to 04:00 boundary — 1 completed bar (00-04 period)
    assert count[4] == count[5] == count[6] == count[7] == 1
    # Hours 8-11 belong to 08:00 boundary — 2 completed bars
    assert count[8] == count[9] == count[10] == count[11] == 2

    # The first completed 4h close should be the close of hour-3 candle
    assert abs(ctx["closes"][0] - (100.0 + 3 * 0.01)) < 1e-9


def test_simulate_forward_trade_times_out_without_tp_or_sl():
    candles = [make_candle(100, 101, 99, 100, idx) for idx in range(25)]
    outcome = analyzer.simulate_forward_trade(
        candles,
        signal_idx=19,
        direction="LONG",
        sl_atr_mult=5.0,
        rr_ratio=5.0,
        max_holding_candles=3,
        fee_pct=0.0,
        slippage_pct=0.0,
    )
    assert outcome is not None
    assert outcome["exit_reason"] == "TIMEOUT"
    assert outcome["is_win"] is False


def test_evaluate_subset_returns_edge_metrics_for_matching_rows():
    rows = [
        {
            "rsi": 35,
            "idx": 0,
            "close_time": 1_700_000_000_000,
            "month": "2025-01",
            "outcomes": {"standard_long": {"is_win": True, "pnl_pct": 2.0}},
        },
        {
            "rsi": 38,
            "idx": 1,
            "close_time": 1_700_000_000_000 + 3_600_000,
            "month": "2025-01",
            "outcomes": {"standard_long": {"is_win": True, "pnl_pct": 1.5}},
        },
        {
            "rsi": 60,
            "idx": 2,
            "close_time": 1_700_000_000_000 + 2 * 3_600_000,
            "month": "2025-01",
            "outcomes": {"standard_long": {"is_win": False, "pnl_pct": -1.0}},
        },
        {
            "rsi": 62,
            "idx": 3,
            "close_time": 1_700_000_000_000 + 3 * 3_600_000,
            "month": "2025-01",
            "outcomes": {"standard_long": {"is_win": False, "pnl_pct": -1.2}},
        },
    ]
    baseline = analyzer._compute_baseline(rows, template_name="standard", direction="LONG", cooldown=0)
    result = analyzer.evaluate_subset(
        rows=rows,
        template_name="standard",
        direction="LONG",
        condition_names=["rsi_below_40"],
        baseline_stats=baseline,
        min_trades=2,
        cooldown=0,
    )
    assert result is not None
    assert result["count"] == 2
    assert result["win_rate"] == 100.0
    assert result["edge_win_rate"] > 0


def test_select_non_overlapping_uses_per_symbol_cooldown_spacing():
    rows = [
        {
            "idx": 0,
            "close_time": 1_700_000_000_000,
            "symbol": "BTCUSDT",
            "outcomes": {"standard_long": {"is_win": True, "pnl_pct": 1.0}},
        },
        {
            "idx": 1,
            "close_time": 1_700_000_000_000 + 3_600_000,
            "symbol": "ETHUSDT",
            "outcomes": {"standard_long": {"is_win": False, "pnl_pct": -1.0}},
        },
        {
            "idx": 2,
            "close_time": 1_700_000_000_000 + 60 * 3_600_000,
            "symbol": "BTCUSDT",
            "outcomes": {"standard_long": {"is_win": True, "pnl_pct": 2.0}},
        },
    ]
    selected = analyzer._select_non_overlapping(rows, "standard_long", cooldown=48)
    assert len(selected) == 3
    assert selected[0]["pnl_pct"] == 1.0
    assert selected[1]["pnl_pct"] == -1.0
    assert selected[2]["pnl_pct"] == 2.0


def test_compute_baseline_keeps_independent_symbol_samples():
    rows = [
        {
            "idx": 0,
            "close_time": 1_700_000_000_000,
            "symbol": "BTCUSDT",
            "outcomes": {"standard_long": {"is_win": True, "pnl_pct": 1.0}},
        },
        {
            "idx": 1,
            "close_time": 1_700_000_000_000 + 3_600_000,
            "symbol": "ETHUSDT",
            "outcomes": {"standard_long": {"is_win": False, "pnl_pct": -1.0}},
        },
        {
            "idx": 2,
            "close_time": 1_700_000_000_000 + 60 * 3_600_000,
            "symbol": "BTCUSDT",
            "outcomes": {"standard_long": {"is_win": True, "pnl_pct": 2.0}},
        },
    ]

    baseline = analyzer._compute_baseline(rows, template_name="standard", direction="LONG", cooldown=48)

    assert baseline.trades == 3
    assert baseline.wins == 2
    assert baseline.losses == 1


def test_per_symbol_breakdown_uses_same_sampling_rules():
    rows = [
        {
            "symbol": "BTCUSDT",
            "idx": 0,
            "close_time": 1_700_000_000_000,
            "rsi": 35,
            "outcomes": {"standard_long": {"is_win": True, "pnl_pct": 1.5}},
        },
        {
            "symbol": "ETHUSDT",
            "idx": 1,
            "close_time": 1_700_000_000_000 + 60 * 3_600_000,
            "rsi": 36,
            "outcomes": {"standard_long": {"is_win": False, "pnl_pct": -0.5}},
        },
    ]
    breakdown = analyzer._per_symbol_breakdown(
        rows,
        template_name="standard",
        direction="LONG",
        condition_names=["rsi_below_40"],
        cooldown=0,
    )
    assert breakdown["BTCUSDT"]["trades"] == 1
    assert breakdown["BTCUSDT"]["win_rate"] == 100.0
    assert breakdown["ETHUSDT"]["trades"] == 1


def make_validated_setup(
    template,
    direction,
    conditions,
    train_stats=None,
    test_stats=None,
    by_symbol=None,
):
    train_stats = train_stats or {
        "count": 20,
        "win_rate": 60.0,
        "avg_pnl_pct": 1.0,
        "profit_factor": 1.8,
        "edge_win_rate": 15.0,
    }
    test_stats = test_stats or {
        "count": 18,
        "win_rate": 58.0,
        "avg_pnl_pct": 0.8,
        "profit_factor": 1.6,
        "edge_win_rate": 12.0,
    }
    by_symbol = by_symbol or {
        "BTCUSDT": {
            "trades": test_stats["count"],
            "win_rate": test_stats["win_rate"],
            "avg_pnl_pct": test_stats["avg_pnl_pct"],
            "profit_factor": test_stats["profit_factor"],
        }
    }
    return {
        "name": analyzer._setup_name(template, direction, conditions),
        "template": template,
        "direction": direction,
        "conditions": conditions,
        "train_stats": train_stats,
        "test_stats": test_stats,
        "tp_sl": dict(analyzer.SETUP_TEMPLATES[template]),
        "by_symbol": by_symbol,
        "window_results": [
            {
                "train_months": ["2025-01", "2025-02"],
                "test_months": ["2025-03"],
                "train_stats": train_stats,
                "test_stats": test_stats,
                "passed": True,
            }
        ],
    }


def test_dedupe_validated_setups_drops_redundant_superset_with_identical_signature():
    simpler = make_validated_setup("standard", "LONG", ["rsi_above_70"])
    redundant = make_validated_setup("standard", "LONG", ["rsi_above_70", "rsi_above_60"])

    kept, removed = analyzer.dedupe_validated_setups([redundant, simpler])

    assert [setup["name"] for setup in kept] == ["standard_long_rsi_above_70"]
    assert removed[0]["reason"] == "redundant_superset"
    assert removed[0]["kept_setup"] == "standard_long_rsi_above_70"


def test_dedupe_validated_setups_keeps_superset_with_material_increment():
    simpler = make_validated_setup(
        "wide",
        "SHORT",
        ["rsi_below_30"],
        test_stats={
            "count": 24,
            "win_rate": 54.17,
            "avg_pnl_pct": 1.5208,
            "profit_factor": 2.5367,
            "edge_win_rate": 24.62,
        },
    )
    stronger = make_validated_setup(
        "wide",
        "SHORT",
        ["rsi_below_30", "vol_above_1_2"],
        train_stats={
            "count": 21,
            "win_rate": 55.0,
            "avg_pnl_pct": 1.5,
            "profit_factor": 2.2,
            "edge_win_rate": 26.0,
        },
        test_stats={
            "count": 21,
            "win_rate": 61.9,
            "avg_pnl_pct": 2.1202,
            "profit_factor": 3.7079,
            "edge_win_rate": 32.36,
        },
    )

    kept, removed = analyzer.dedupe_validated_setups([simpler, stronger])

    assert len(kept) == 2
    assert removed == []


def test_build_validated_setups_export_creates_lightweight_schema():
    symbol_scoped_short = make_validated_setup("wide", "SHORT", ["rsi_below_30"])
    symbol_scoped_short["scope_key"] = "symbol_BTCUSDT"
    symbol_scoped_short["scope_type"] = "symbol"
    symbol_scoped_short["scope_symbol"] = "BTCUSDT"
    symbol_scoped_short["filter"] = {"symbol": "BTCUSDT"}
    report = {
        "metadata": {
            "generated_at": "2026-03-30T00:00:00+00:00",
            "data_range": "2025-04 to 2026-03",
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "train_months": 6,
            "test_months": 3,
            "step_months": 3,
            "eval_cooldown_candles": 48,
            "fee_pct": 0.1,
            "slippage_pct": 0.05,
        },
        "validated_setups": {
            "long": [make_validated_setup("standard", "LONG", ["rsi_above_70"])],
            "short": [symbol_scoped_short],
        },
        "dedupe_removed_setups": {"long": [], "short": [{"name": "x"}]},
    }

    exported = analyzer.build_validated_setups_export(report)

    assert exported["schema_version"] == 1
    assert exported["metadata"]["dedupe_policy"]["removed_counts"]["short"] == 1
    assert exported["validated_setups"]["long"][0]["template"] == "standard"
    assert exported["validated_setups"]["long"][0]["conditions"] == ["rsi_above_70"]
    assert exported["validated_setups"]["short"][0]["template"] == "wide"
    assert exported["validated_setups"]["short"][0]["conditions"] == ["rsi_below_30"]
    assert exported["validated_setups"]["short"][0]["filter"] == {"symbol": "BTCUSDT"}
    assert "window_results" not in exported["validated_setups"]["long"][0]


def test_validate_candidates_records_all_windows_before_rejecting():
    rows = [
        {
            "symbol": "BTCUSDT",
            "idx": 0,
            "close_time": 1_700_000_000_000,
            "month": "2025-01",
            "rsi": 80,
            "outcomes": {"standard_long": {"is_win": True, "pnl_pct": 2.0}},
        },
        {
            "symbol": "BTCUSDT",
            "idx": 10,
            "close_time": 1_700_036_000_000,
            "month": "2025-02",
            "rsi": 81,
            "outcomes": {"standard_long": {"is_win": True, "pnl_pct": 2.0}},
        },
        {
            "symbol": "BTCUSDT",
            "idx": 20,
            "close_time": 1_700_072_000_000,
            "month": "2025-03",
            "rsi": 82,
            "outcomes": {"standard_long": {"is_win": False, "pnl_pct": -2.0}},
        },
        {
            "symbol": "BTCUSDT",
            "idx": 30,
            "close_time": 1_700_108_000_000,
            "month": "2025-04",
            "rsi": 83,
            "outcomes": {"standard_long": {"is_win": False, "pnl_pct": -2.0}},
        },
    ]

    validated, rejected = analyzer.validate_candidates(
        rows=rows,
        template_name="standard",
        direction="LONG",
        candidate_results=[{"conditions": ["rsi_above_70"]}],
        train_months=1,
        test_months=1,
        step_months=1,
        min_train_trades=1,
        min_test_trades=1,
        validation_p=1.0,
        cooldown=0,
    )

    assert validated == []
    # With majority-pass logic (at most 1 failure allowed per N windows),
    # 0-of-3 passing produces the insufficient_windows_passed reason.
    assert "insufficient_windows_passed" in rejected[0]["reason"] or rejected[0]["reason"] == "failed_out_of_sample"
    assert rejected[0]["windows_total"] == 3
    assert rejected[0]["windows_passed"] == 0
    assert len(rejected[0]["window_results"]) == 3


def test_build_analysis_views_creates_pooled_and_symbol_scopes():
    rows = [
        {"symbol": "BTCUSDT"},
        {"symbol": "BTCUSDT"},
        {"symbol": "ETHUSDT"},
    ]

    views = analyzer.build_analysis_views(
        rows,
        symbols=["BTCUSDT", "ETHUSDT"],
        discovery_variants=["pooled", "symbol_specific"],
    )

    assert "pooled" in views
    assert views["pooled"]["scope"]["scope_type"] == "pooled"
    assert "symbol_BTCUSDT" in views
    assert views["symbol_BTCUSDT"]["scope"]["scope_symbol"] == "BTCUSDT"
    assert "symbol_ETHUSDT" in views


def test_validate_candidates_attaches_scope_context():
    rows = [
        {
            "symbol": "BTCUSDT",
            "idx": 0,
            "close_time": 1_700_000_000_000,
            "month": "2025-01",
            "rsi": 80,
            "outcomes": {"standard_long": {"is_win": True, "pnl_pct": 2.0}},
        },
        {
            "symbol": "BTCUSDT",
            "idx": 10,
            "close_time": 1_700_036_000_000,
            "month": "2025-01",
            "rsi": 45,
            "outcomes": {"standard_long": {"is_win": False, "pnl_pct": -1.5}},
        },
        {
            "symbol": "BTCUSDT",
            "idx": 50,
            "close_time": 1_700_180_000_000,
            "month": "2025-02",
            "rsi": 81,
            "outcomes": {"standard_long": {"is_win": True, "pnl_pct": 1.8}},
        },
        {
            "symbol": "BTCUSDT",
            "idx": 60,
            "close_time": 1_700_216_000_000,
            "month": "2025-02",
            "rsi": 46,
            "outcomes": {"standard_long": {"is_win": False, "pnl_pct": -1.6}},
        },
        {
            "symbol": "BTCUSDT",
            "idx": 100,
            "close_time": 1_700_360_000_000,
            "month": "2025-03",
            "rsi": 82,
            "outcomes": {"standard_long": {"is_win": True, "pnl_pct": 2.1}},
        },
        {
            "symbol": "BTCUSDT",
            "idx": 110,
            "close_time": 1_700_396_000_000,
            "month": "2025-03",
            "rsi": 47,
            "outcomes": {"standard_long": {"is_win": False, "pnl_pct": -1.4}},
        },
    ]

    validated, rejected = analyzer.validate_candidates(
        rows=rows,
        template_name="standard",
        direction="LONG",
        candidate_results=[{"conditions": ["rsi_above_70"]}],
        train_months=2,
        test_months=1,
        step_months=1,
        min_train_trades=1,
        min_test_trades=1,
        validation_p=1.0,
        cooldown=0,
        scope_context={"scope_key": "symbol_BTCUSDT", "scope_type": "symbol", "scope_symbol": "BTCUSDT"},
    )

    assert rejected == []
    assert validated[0]["scope_key"] == "symbol_BTCUSDT"
    assert validated[0]["scope_symbol"] == "BTCUSDT"
