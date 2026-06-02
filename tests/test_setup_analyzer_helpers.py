import json
from datetime import datetime, timezone

from trading_bot import setup_analyzer as analyzer


def test_normalize_template_name_maps_legacy_aliases():
    assert analyzer._normalize_template_name("trend") == "standard"
    assert analyzer._normalize_template_name("breakout") == "wide"
    assert analyzer._normalize_template_name("standard") == "standard"


def test_calculate_atr_returns_zero_for_short_input():
    assert analyzer.calculate_atr([]) == 0.0
    assert analyzer.calculate_atr([{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]) == 0.0


def test_build_adx_series_returns_zeroes_for_short_input():
    series = analyzer._build_adx_series([1.0] * 10, [1.0] * 10, [1.0] * 10)
    assert series == [0.0] * 10


def test_build_volume_ratio_series_uses_trailing_average():
    series = analyzer._build_volume_ratio_series([10.0] * 20 + [20.0], period=20)
    assert series[0] == 1.0
    assert series[-1] == 2.0


def test_build_bollinger_context_computes_bandwidth_and_squeeze():
    closes = [100.0] * 25 + [100.1, 100.0, 99.9, 100.0, 100.0]
    context = analyzer._build_bollinger_context(closes, period=20, squeeze_lookback=5)

    assert len(context["bb_mid"]) == len(closes)
    assert len(context["bb_squeeze"]) == len(closes)
    assert context["bb_bandwidth"][-1] >= 0.0


def test_build_atr_series_returns_zeroes_for_short_input():
    assert analyzer._build_atr_series([]) == []
    one = [{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}]
    assert analyzer._build_atr_series(one) == [0.0]


def test_resolve_same_candle_hit_is_conservative_on_equidistant_open():
    # Analyzer wrapper uses tie_break="sl" for research determinism.
    # When open is equidistant from TP and SL, must always pick SL.
    candle = {"open": 102.5}  # midpoint between SL=95 and TP=110
    assert analyzer.resolve_same_candle_hit(candle, tp_price=110.0, sl_price=95.0) == "SL"
    # SHORT: equidistant between TP=90 and SL=105 is 97.5
    candle_short = {"open": 97.5}
    assert analyzer.resolve_same_candle_hit(candle_short, tp_price=90.0, sl_price=105.0) == "SL"


def test_metric_value_handles_infinite_profit_factor():
    assert analyzer._metric_value({"profit_factor": float("inf")}, "profit_factor") == 1_000_000.0
    assert analyzer._metric_value({"profit_factor": 1.7}, "profit_factor") == 1.7


def test_average_window_metric_falls_back_to_summary_stats():
    setup = {"test_stats": {"avg_pnl_pct": 1.25}}
    assert analyzer._average_window_metric(setup, "test", "avg_pnl_pct") == 1.25


def test_has_material_increment_detects_meaningful_improvement():
    base = {
        "train_stats": {"avg_pnl_pct": 0.5, "profit_factor": 1.2},
        "test_stats": {"avg_pnl_pct": 0.5, "profit_factor": 1.2, "edge_win_rate": 5.0},
        "window_results": [],
    }
    stronger = {
        "train_stats": {"avg_pnl_pct": 0.8, "profit_factor": 1.5},
        "test_stats": {"avg_pnl_pct": 0.8, "profit_factor": 1.6, "edge_win_rate": 9.0},
        "window_results": [],
    }
    assert analyzer._has_material_increment(stronger, base) is True


def test_export_runtime_setup_keeps_regime_scope():
    setup = {
        "name": "wide_short_rsi_below_30",
        "template": "breakout",
        "direction": "SHORT",
        "conditions": ["rsi_below_30"],
        "tp_sl": {"sl_atr_mult": 2.0, "rr_ratio": 2.5},
        "train_stats": {},
        "test_stats": {},
        "by_symbol": {},
        "scope_context": {"scope_type": "regime", "scope_regime": "bull"},
        "scope_key": "regime_bull",
    }

    exported = analyzer._export_runtime_setup(setup)

    assert exported["template"] == "wide"
    assert exported["scope_key"] == "regime_bull"
    assert exported["filter"] == {"regime": "bull"}


def test_normalize_discovery_variants_dedupes_and_defaults():
    assert analyzer._normalize_discovery_variants(["POOLED", "pooled", "regime", "bad"]) == ["pooled", "regime"]
    assert analyzer._normalize_discovery_variants(["bad"]) == ["pooled"]
    assert analyzer._normalize_discovery_variants(None) == ["pooled"]


def test_tag_rows_with_regime_handles_missing_btc_data():
    rows = [{"close_time": 1}, {"close_time": 2, "regime": "old"}]
    analyzer.tag_rows_with_regime(rows, [])
    assert rows[0]["regime"] == "unknown"
    assert rows[1]["regime"] == "old"


def test_tag_rows_with_regime_uses_lookup(monkeypatch):
    rows = [{"close_time": 100}, {"close_time": 200}]
    monkeypatch.setattr(analyzer, "build_regime_lookup", lambda candles: {100: "bull"})

    analyzer.tag_rows_with_regime(rows, [{"close_time": 100}])

    assert rows[0]["regime"] == "bull"
    assert rows[1]["regime"] == "unknown"


def test_build_analysis_views_supports_regime_scope():
    rows = [
        {"symbol": "BTCUSDT", "regime": "bull"},
        {"symbol": "ETHUSDT", "regime": "bear"},
        {"symbol": "BTCUSDT", "regime": "bull"},
    ]

    views = analyzer.build_analysis_views(rows, ["BTCUSDT", "ETHUSDT"], ["pooled", "regime"])

    assert "pooled" in views
    assert "regime_bull" in views
    assert views["regime_bull"]["scope"]["scope_regime"] == "bull"
    assert views["regime_bear"]["scope"]["filter"] == {"regime": "bear"}


def test_load_symbol_universe_rows_single_worker(monkeypatch):
    calls = []

    def fake_fetch(symbol, interval, start_ms, end_ms, use_cache=True):
        calls.append(("fetch", symbol, interval, use_cache))
        return [{"close_time": 1}] * (analyzer.DEFAULT_WARMUP_CANDLES + 150)

    def fake_build(**kwargs):
        return [{"symbol": kwargs["symbol"], "close_time": 1}]

    monkeypatch.setattr(analyzer, "fetch_klines_historical_cached", fake_fetch)
    monkeypatch.setattr(analyzer, "build_symbol_rows", fake_build)

    all_rows, rows_by_symbol, candles_by_symbol = analyzer.load_symbol_universe_rows(
        symbols=["BTCUSDT"],
        start_ms=1,
        end_ms=2,
        analysis_start_ms=1,
        warmup_candles=analyzer.DEFAULT_WARMUP_CANDLES,
        max_holding_candles=10,
        fee_pct=0.1,
        slippage_pct=0.05,
        workers=1,
    )

    assert all_rows == [{"symbol": "BTCUSDT", "close_time": 1}]
    assert rows_by_symbol["BTCUSDT"] == all_rows
    assert len(candles_by_symbol["BTCUSDT"]) == analyzer.DEFAULT_WARMUP_CANDLES + 150
    assert calls


def test_load_and_build_symbol_rows_skips_insufficient_candles(monkeypatch):
    warnings = []
    monkeypatch.setattr(analyzer, "fetch_klines_historical_cached", lambda *args, **kwargs: [{"close_time": 1}] * 10)
    monkeypatch.setattr(analyzer.log, "warning", lambda msg: warnings.append(msg))

    symbol, rows = analyzer.load_and_build_symbol_rows(
        symbol="BTCUSDT",
        start_ms=0,
        end_ms=1,
        analysis_start_ms=0,
        warmup_candles=analyzer.DEFAULT_WARMUP_CANDLES,
        max_holding_candles=10,
        fee_pct=0.1,
        slippage_pct=0.05,
    )

    assert symbol == "BTCUSDT"
    assert rows == []
    assert warnings


def test_load_and_build_symbol_rows_builds_rows(monkeypatch):
    candles = [{"close_time": 1}] * (analyzer.DEFAULT_WARMUP_CANDLES + 120)
    monkeypatch.setattr(analyzer, "fetch_klines_historical_cached", lambda *args, **kwargs: candles)
    monkeypatch.setattr(analyzer, "build_symbol_rows", lambda **kwargs: [{"symbol": kwargs["symbol"], "close_time": 1}])

    symbol, rows = analyzer.load_and_build_symbol_rows(
        symbol="ETHUSDT",
        start_ms=0,
        end_ms=1,
        analysis_start_ms=0,
        warmup_candles=analyzer.DEFAULT_WARMUP_CANDLES,
        max_holding_candles=10,
        fee_pct=0.1,
        slippage_pct=0.05,
    )

    assert symbol == "ETHUSDT"
    assert rows == [{"symbol": "ETHUSDT", "close_time": 1}]


def test_analyze_rows_scope_aggregates_validation_and_dedupe(monkeypatch):
    monkeypatch.setattr(
        analyzer,
        "analyze_conditions",
        lambda **kwargs: ({"baseline": {}, "individual_conditions": [], "combinations": []}, [{"conditions": ["rsi_below_30"]}]),
    )
    monkeypatch.setattr(
        analyzer,
        "validate_candidates",
        lambda **kwargs: (
            [{"name": "setup1", "template": "standard", "direction": kwargs["direction"], "conditions": ["rsi_below_30"], "test_stats": {}, "train_stats": {}, "window_results": [], "by_symbol": {}}],
            [{"name": "rej"}],
        ),
    )
    monkeypatch.setattr(
        analyzer,
        "dedupe_validated_setups",
        lambda setups: (list(setups), [{"name": "removed"}]),
    )

    report = analyzer.analyze_rows_scope(
        rows=[{"symbol": "BTCUSDT"}],
        template_names=["standard"],
        combo_max_size=2,
        max_combo_tests=5,
        min_train_trades=1,
        min_test_trades=1,
        prefilter_p=0.1,
        validation_p=0.05,
        train_months=6,
        test_months=3,
        step_months=3,
        scope_context={"scope_key": "pooled", "scope_type": "pooled"},
    )

    assert report["summary"]["validated_long"] == 1
    assert report["summary"]["validated_short"] == 1
    assert report["summary"]["rejected"] == 2
    assert report["dedupe_removed_setups"]["long"] == [{"name": "removed"}]


def test_select_diverse_promising_drops_same_family_duplicates():
    ordered = ["rsi_below_28", "rsi_below_30", "macd_hist_falling", "us_session"]
    selected = analyzer._select_diverse_promising(ordered)
    assert "rsi_below_28" in selected
    assert "rsi_below_30" not in selected
    assert "macd_hist_falling" in selected


def test_analyze_conditions_applies_bh_prefilter_and_combo_generation(monkeypatch):
    monkeypatch.setattr(analyzer, "ALL_CONDITIONS", ["rsi_below_28", "macd_hist_falling", "us_session"])
    monkeypatch.setattr(
        analyzer,
        "_compute_baseline",
        lambda *args, **kwargs: analyzer.SampleStats(
            trades=10,
            wins=5,
            losses=5,
            win_rate=50.0,
            avg_pnl_pct=0.2,
            profit_factor=1.1,
            pnl_sum_pct=2.0,
            wilson_low=0.4,
            wilson_high=0.6,
        ),
    )

    def fake_evaluate(rows, template_name, direction, condition_names, baseline_stats, min_trades, cooldown):
        name = condition_names[0]
        if len(condition_names) == 1:
            p_values = {"rsi_below_28": 0.01, "macd_hist_falling": 0.02, "us_session": 0.5}
            avg_pnls = {"rsi_below_28": 1.0, "macd_hist_falling": 0.8, "us_session": -0.1}
            return {
                "conditions": condition_names,
                "p_value": p_values[name],
                "avg_pnl_pct": avg_pnls[name],
                "edge_avg_pnl_pct": avg_pnls[name],
            }
        return {
            "conditions": condition_names,
            "p_value": 0.03,
            "avg_pnl_pct": 1.2,
            "edge_avg_pnl_pct": 1.2,
        }

    monkeypatch.setattr(analyzer, "evaluate_subset", fake_evaluate)
    monkeypatch.setattr(analyzer, "benjamini_hochberg_adjusted", lambda values: values)

    analysis, combos = analyzer.analyze_conditions(
        rows=[{"symbol": "BTCUSDT"}],
        template_name="standard",
        direction="LONG",
        min_trades=1,
        combo_max_size=2,
        max_combo_tests=5,
        prefilter_p=0.05,
    )

    assert analysis["baseline"]["trades"] == 10
    assert len(analysis["individual_conditions"]) == 3
    assert analysis["individual_conditions"][0]["bh_adj_p_value"] == 0.01
    assert combos


def test_analyze_symbol_universe_builds_metadata_and_variants(monkeypatch):
    rows = [{"symbol": "BTCUSDT", "close_time": 100, "regime": "bull"}]
    monkeypatch.setattr(analyzer, "load_symbol_universe_rows", lambda **kwargs: (rows, {"BTCUSDT": rows}, {"BTCUSDT": [{"close_time": 100}]}))
    monkeypatch.setattr(analyzer, "tag_rows_with_regime", lambda all_rows, btc_candles: None)
    monkeypatch.setattr(
        analyzer,
        "analyze_rows_scope",
        lambda **kwargs: {
            "analysis": {"x": {}},
            "validated_setups": {"long": [], "short": []},
            "raw_validated_setups": {"long": [], "short": []},
            "dedupe_removed_setups": {"long": [], "short": []},
            "rejected_setups": [],
            "summary": {"validated_long": 0, "validated_short": 0, "rejected": 0},
        },
    )
    monkeypatch.setattr(
        analyzer,
        "build_analysis_views",
        lambda rows, symbols, discovery_variants: {
            "pooled": {"rows": rows, "template_names": ["standard"], "scope": {"scope_key": "pooled", "scope_type": "pooled"}},
            "regime_bull": {"rows": rows, "template_names": ["standard"], "scope": {"scope_key": "regime_bull", "scope_type": "regime", "scope_regime": "bull"}},
        },
    )

    report = analyzer.analyze_symbol_universe(
        symbols=["BTCUSDT"],
        months=12,
        warmup_candles=analyzer.DEFAULT_WARMUP_CANDLES,
        max_holding_candles=analyzer.DEFAULT_MAX_HOLDING_CANDLES,
        combo_max_size=2,
        max_combo_tests=5,
        min_train_trades=1,
        min_test_trades=1,
        fee_pct=0.1,
        slippage_pct=0.05,
        train_months=6,
        test_months=3,
        step_months=3,
        prefilter_p=0.1,
        validation_p=0.05,
        end_dt=datetime(2026, 4, 25, tzinfo=timezone.utc),
        workers=1,
        discovery_variants=["pooled", "regime"],
    )

    assert report["metadata"]["symbols"] == ["BTCUSDT"]
    assert report["metadata"]["workers"] == 1
    assert report["metadata"]["discovery_variants"] == ["pooled", "regime"]
    assert "discovery_variants" in report
    assert "regime_bull" in report["discovery_variants"]


def test_main_writes_report_and_validated_outputs(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    validated_path = tmp_path / "validated.json"
    info_logs = []

    monkeypatch.setattr(
        analyzer,
        "analyze_symbol_universe",
        lambda **kwargs: {
            "analysis": {"pooled": {}},
            "validated_setups": {"long": [{"name": "rule_a"}], "short": []},
            "raw_validated_setups": {"long": [], "short": []},
            "dedupe_removed_setups": {"long": [], "short": []},
            "rejected_setups": [],
            "summary": {"validated_long": 1, "validated_short": 0, "rejected": 0},
        },
    )
    monkeypatch.setattr(
        analyzer,
        "build_validated_setups_export",
        lambda report: {
            "generated_at": "2026-04-25T00:00:00+00:00",
            "validated_setups": report["validated_setups"],
        },
    )
    monkeypatch.setattr(analyzer.log, "info", lambda msg, *args: info_logs.append(msg % args if args else msg))
    monkeypatch.setattr(
        analyzer.sys,
        "argv",
        [
            "setup_analyzer.py",
            "--months",
            "18",
            "--symbols",
            "BTCUSDT",
            "ETHUSDT",
            "--workers",
            "1",
            "--output",
            str(report_path),
            "--validated-output",
            str(validated_path),
            "--end-date",
            "2025-10-18",
            "--discovery-variants",
            "pooled",
            "regime",
        ],
    )

    result = analyzer.main()

    assert result == 0
    report = json.loads(report_path.read_text())
    validated = json.loads(validated_path.read_text())
    assert report["summary"]["validated_long"] == 1
    assert validated["validated_setups"]["long"][0]["name"] == "rule_a"
    assert any("Saved analysis to" in line for line in info_logs)
    assert any("Saved validated setups to" in line for line in info_logs)
