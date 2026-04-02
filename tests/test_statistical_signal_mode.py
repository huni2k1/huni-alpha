import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

scanner_path = os.path.join(os.path.dirname(__file__), "..", "src", "trading_bot", "scanner.py")
scanner_spec = importlib.util.spec_from_file_location("stat_scanner", scanner_path)
scanner = importlib.util.module_from_spec(scanner_spec)
scanner_spec.loader.exec_module(scanner)

backtester_path = os.path.join(os.path.dirname(__file__), "..", "src", "trading_bot", "backtester.py")
backtester_spec = importlib.util.spec_from_file_location("stat_backtester", backtester_path)
backtester = importlib.util.module_from_spec(backtester_spec)
backtester_spec.loader.exec_module(backtester)

REPO_VALIDATED_SETUPS = Path(scanner_path).with_name("validated_setups.json")
REPO_CURATED_SETUPS = Path(scanner_path).with_name("curated_statistical_setups.json")


def make_trending_candles(n=100, direction="up"):
    candles = []
    price = 100.0
    for _ in range(n):
        price += 0.15 if direction == "up" else -0.15
        candles.append([price - 0.05, price + 0.10, price - 0.10, price, 1000.0])
    return candles


def make_flat_candles_dict(n, price=100.0, volume=1000.0):
    candles = []
    for i in range(n):
        open_time_ms = 1_700_000_000_000 + (i * 3_600_000)
        close_time_ms = open_time_ms + 3_600_000 - 1
        candles.append(
            {
                "open_time": open_time_ms,
                "close_time": close_time_ms,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": volume,
            }
        )
    return candles


def write_validated_export(path):
    payload = {
        "schema_version": 1,
        "metadata": {},
        "validated_setups": {
            "long": [
                {
                    "name": "wide_long_rsi_above_70",
                    "template": "wide",
                    "direction": "LONG",
                    "conditions": ["bb_squeeze", "rsi_above_70", "vol_above_1_5"],
                    "tp_sl": {"sl_atr_mult": 2.0, "rr_ratio": 2.5},
                    "train_stats": {"count": 30, "win_rate": 60.0, "avg_pnl_pct": 0.9, "profit_factor": 1.7, "edge_win_rate": 12.0},
                    "test_stats": {"count": 21, "win_rate": 47.62, "avg_pnl_pct": 0.4668, "profit_factor": 1.3947, "edge_win_rate": 20.35},
                    "by_symbol": {"BTCUSDT": {"trades": 5, "win_rate": 60.0, "avg_pnl_pct": 0.5, "profit_factor": 1.5}},
                }
            ],
            "short": [
                {
                    "name": "wide_short_rsi_below_30",
                    "template": "wide",
                    "direction": "SHORT",
                    "conditions": ["bb_squeeze", "rsi_below_30", "vol_above_1_5"],
                    "tp_sl": {"sl_atr_mult": 2.0, "rr_ratio": 2.5},
                    "train_stats": {"count": 51, "win_rate": 49.0, "avg_pnl_pct": 0.84, "profit_factor": 1.63, "edge_win_rate": 24.0},
                    "test_stats": {"count": 24, "win_rate": 54.17, "avg_pnl_pct": 1.5208, "profit_factor": 2.5367, "edge_win_rate": 24.62},
                    "by_symbol": {"BTCUSDT": {"trades": 6, "win_rate": 55.0, "avg_pnl_pct": 1.0, "profit_factor": 2.0}},
                }
            ],
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_curated_scope_export(path):
    payload = {
        "schema_version": 1,
        "metadata": {},
        "validated_setups": {
            "long": [],
            "short": [
                {
                    "name": "standard_short_rsi_below_40_pooled",
                    "template": "standard",
                    "direction": "SHORT",
                    "conditions": ["rsi_below_40"],
                    "tp_sl": {"sl_atr_mult": 1.5, "rr_ratio": 2.0},
                    "train_stats": {"count": 40, "win_rate": 55.0, "avg_pnl_pct": 0.7, "profit_factor": 1.8, "edge_win_rate": 15.0},
                    "test_stats": {"count": 20, "win_rate": 55.0, "avg_pnl_pct": 0.8, "profit_factor": 1.9, "edge_win_rate": 18.0},
                    "by_symbol": {},
                    "scope_key": "pooled",
                    "scope_type": "pooled"
                },
                {
                    "name": "standard_short_rsi_below_40_avax_weak",
                    "template": "standard",
                    "direction": "SHORT",
                    "conditions": ["adx_below_25", "rsi_below_40"],
                    "tp_sl": {"sl_atr_mult": 1.5, "rr_ratio": 2.0},
                    "train_stats": {"count": 35, "win_rate": 58.0, "avg_pnl_pct": 1.0, "profit_factor": 2.2, "edge_win_rate": 22.0},
                    "test_stats": {"count": 18, "win_rate": 66.0, "avg_pnl_pct": 1.2, "profit_factor": 2.5, "edge_win_rate": 30.0},
                    "by_symbol": {"AVAXUSDT": {"trades": 18, "win_rate": 66.0, "avg_pnl_pct": 1.2, "profit_factor": 2.5}},
                    "scope_key": "symbol_AVAXUSDT",
                    "scope_type": "symbol",
                    "scope_symbol": "AVAXUSDT"
                }
            ],
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_generate_signal_statistical_matches_validated_setup(tmp_path):
    validated_path = tmp_path / "validated_setups.json"
    write_validated_export(validated_path)
    candles = make_trending_candles(120, "up")
    with patch.object(
        scanner,
        "_build_statistical_snapshot",
        return_value={"rsi": 72, "hour_utc": 12, "bb_squeeze": True, "vol_ratio": 1.6},
    ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            include_fundamentals=False,
            include_news=False,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="statistical",
            validated_setups_path=str(validated_path),
        )

    assert signal is not None
    assert signal["signal_model"] == "statistical"
    assert signal["direction"] == "LONG"
    assert signal["strategy"] == "statistical_wide"
    assert signal["statistical_setup"] == "wide_long_rsi_above_70"
    assert signal["score"] == 1.3947


def test_generate_signal_statistical_returns_none_without_matching_setup(tmp_path):
    validated_path = tmp_path / "validated_setups.json"
    write_validated_export(validated_path)
    candles = make_trending_candles(120, "up")
    with patch.object(
        scanner,
        "_build_statistical_snapshot",
        return_value={"rsi": 55, "hour_utc": 12, "bb_squeeze": True, "vol_ratio": 1.6},
    ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            include_fundamentals=False,
            include_news=False,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="statistical",
            validated_setups_path=str(validated_path),
        )

    assert signal is None


def test_generate_signal_dedicated_wide_short_rsi28_matches():
    candles = make_trending_candles(120, "down")
    with patch.object(
        scanner,
        "_build_statistical_snapshot",
        return_value={"rsi": 27.5, "hour_utc": 12},
    ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            include_fundamentals=False,
            include_news=False,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="statistical_wide_short_rsi28",
        )

    assert signal is not None
    assert signal["signal_model"] == "statistical_wide_short_rsi28"
    assert signal["direction"] == "SHORT"
    assert signal["strategy"] == "statistical_wide_short_rsi28"
    assert signal["statistical_setup"] == "wide_short_rsi_below_28"


def test_generate_signal_dedicated_wide_short_rsi28_returns_none_without_match():
    candles = make_trending_candles(120, "down")
    with patch.object(
        scanner,
        "_build_statistical_snapshot",
        return_value={"rsi": 29.0, "hour_utc": 12},
    ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            include_fundamentals=False,
            include_news=False,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="statistical_wide_short_rsi28",
        )

    assert signal is None


def test_generate_signal_hybrid_exposes_both_evaluations_and_prefers_statistical_short():
    candles = make_trending_candles(120, "down")
    technical_signal = {
        "symbol": "BTCUSDT",
        "direction": "SHORT",
        "score": 7.4,
        "entry_price": 100.0,
        "tp": 95.0,
        "sl": 102.0,
        "tp_pct": 5.0,
        "sl_pct": 2.0,
        "atr": 1.0,
        "rr_ratio": 2.0,
        "technical_score": 7.4,
        "fundamental_score": 0.0,
        "news_score": 0.0,
        "long_score": 2.0,
        "short_score": 7.4,
        "regime": "breakout",
        "strategy": "breakout",
        "details": {"strategy": "breakout", "regime": "breakout"},
        "fund_details": {},
        "news_details": {},
        "signal_model": "technical",
    }
    statistical_signal = {
        "symbol": "BTCUSDT",
        "direction": "SHORT",
        "score": 0.0,
        "entry_price": 100.0,
        "tp": 92.0,
        "sl": 103.0,
        "tp_pct": 8.0,
        "sl_pct": 3.0,
        "atr": 1.0,
        "rr_ratio": 2.5,
        "technical_score": 0.0,
        "fundamental_score": 0.0,
        "news_score": 0.0,
        "long_score": 0.0,
        "short_score": 0.0,
        "regime": "statistical",
        "strategy": "statistical_wide_short_rsi28",
        "details": {"strategy": "statistical_wide_short_rsi28", "regime": "statistical"},
        "fund_details": {},
        "news_details": {},
        "signal_model": "statistical_wide_short_rsi28",
        "statistical_setup": "wide_short_rsi_below_28",
        "statistical_details": {"conditions": ["rsi_below_28"], "template": "wide"},
    }

    with patch.object(scanner, "_generate_technical_signal", return_value=technical_signal), \
         patch.object(scanner, "_generate_dedicated_wide_short_rsi28_signal", return_value=statistical_signal):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            include_fundamentals=False,
            include_news=False,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="hybrid_technical_wide_short_rsi28",
        )

    assert signal is not None
    assert signal["signal_model"] == "hybrid_technical_wide_short_rsi28"
    assert signal["strategy"] == "statistical_wide_short_rsi28"
    assert signal["hybrid_details"]["technical"]["strategy"] == "breakout"
    assert signal["hybrid_details"]["statistical"]["setup"] == "wide_short_rsi_below_28"
    assert signal["hybrid_details"]["selected"]["source"] == "statistical"


def test_generate_signal_hybrid_keeps_technical_long_when_statistical_short_conflicts():
    candles = make_trending_candles(120, "up")
    technical_signal = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "score": 7.6,
        "entry_price": 100.0,
        "tp": 106.0,
        "sl": 97.0,
        "tp_pct": 6.0,
        "sl_pct": 3.0,
        "atr": 1.0,
        "rr_ratio": 2.0,
        "technical_score": 7.6,
        "fundamental_score": 0.0,
        "news_score": 0.0,
        "long_score": 7.6,
        "short_score": 2.0,
        "regime": "trending",
        "strategy": "trend_pullback",
        "details": {"strategy": "trend_pullback", "regime": "trending"},
        "fund_details": {},
        "news_details": {},
        "signal_model": "technical",
    }
    statistical_signal = {
        "symbol": "BTCUSDT",
        "direction": "SHORT",
        "score": 0.0,
        "entry_price": 100.0,
        "tp": 92.0,
        "sl": 103.0,
        "tp_pct": 8.0,
        "sl_pct": 3.0,
        "atr": 1.0,
        "rr_ratio": 2.5,
        "technical_score": 0.0,
        "fundamental_score": 0.0,
        "news_score": 0.0,
        "long_score": 0.0,
        "short_score": 0.0,
        "regime": "statistical",
        "strategy": "statistical_wide_short_rsi28",
        "details": {"strategy": "statistical_wide_short_rsi28", "regime": "statistical"},
        "fund_details": {},
        "news_details": {},
        "signal_model": "statistical_wide_short_rsi28",
        "statistical_setup": "wide_short_rsi_below_28",
        "statistical_details": {"conditions": ["rsi_below_28"], "template": "wide"},
    }

    with patch.object(scanner, "_generate_technical_signal", return_value=technical_signal), \
         patch.object(scanner, "_generate_dedicated_wide_short_rsi28_signal", return_value=statistical_signal):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            include_fundamentals=False,
            include_news=False,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="hybrid_technical_wide_short_rsi28",
        )

    assert signal is not None
    assert signal["direction"] == "LONG"
    assert signal["strategy"] == "trend_pullback"
    assert signal["hybrid_details"]["selected"]["source"] == "technical"


def test_generate_signal_hybrid_reports_both_rejection_reasons_when_no_signal():
    candles = make_trending_candles(120, "up")

    with patch.object(scanner, "_generate_technical_signal", return_value=None), \
         patch.object(scanner, "_generate_dedicated_wide_short_rsi28_signal", return_value=None):
        scanner._last_rejection_reason["BTCUSDT"] = "No validated setup"
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            include_fundamentals=False,
            include_news=False,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="hybrid_technical_wide_short_rsi28",
        )

    assert signal is None
    assert "Hybrid rejected" in scanner._last_rejection_reason["BTCUSDT"]


def test_generate_signal_statistical_curated_prefers_scoped_setup(tmp_path):
    validated_path = tmp_path / "curated_setups.json"
    write_curated_scope_export(validated_path)
    candles = make_trending_candles(120, "down")
    with patch.object(
        scanner,
        "_build_statistical_snapshot",
        return_value={"rsi": 35, "adx": 21, "bb_squeeze": False, "vol_ratio": 1.0, "hour_utc": 12},
    ):
        signal = scanner.generate_signal(
            "AVAXUSDT",
            candles,
            include_fundamentals=False,
            include_news=False,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="statistical_curated",
            validated_setups_path=str(validated_path),
        )

    assert signal is not None
    assert signal["signal_model"] == "statistical_curated"
    assert signal["direction"] == "SHORT"
    assert signal["statistical_setup"] == "standard_short_rsi_below_40_avax_weak"
    assert signal["statistical_details"]["scope_type"] == "symbol"
    assert signal["statistical_details"]["scope_symbol"] == "AVAXUSDT"
    assert signal["statistical_details"]["scope_regime"] is None


def test_load_validated_setups_preserves_indicator_only_rules(tmp_path):
    curated_path = tmp_path / "curated_setups.json"
    validated_path = tmp_path / "validated_setups.json"
    write_curated_scope_export(curated_path)
    write_validated_export(validated_path)

    curated = scanner.load_validated_setups(str(curated_path))
    validated = scanner.load_validated_setups(str(validated_path))
    avax_setup = next(setup for setup in curated["short"] if setup["scope_symbol"] == "AVAXUSDT")
    pooled_trend_setup = next(setup for setup in curated["short"] if setup["scope_type"] == "pooled")
    breakout_setup = validated["short"][0]

    assert "adx_below_25" in avax_setup["conditions"]
    assert avax_setup["scope_type"] == "symbol"
    assert avax_setup["scope_regime"] is None

    assert "bb_squeeze" not in avax_setup["conditions"]
    assert "rsi_below_40" in pooled_trend_setup["conditions"]
    assert "bb_squeeze" in breakout_setup["conditions"]
    assert "vol_above_1_5" in breakout_setup["conditions"]


def test_generate_signal_statistical_ignores_technical_neutral_gate(tmp_path):
    validated_path = tmp_path / "validated_setups.json"
    write_validated_export(validated_path)
    candles = make_trending_candles(120, "up")

    with patch.object(scanner, "score_technical", return_value={"direction": "NEUTRAL"}), \
         patch.object(
             scanner,
             "_build_statistical_snapshot",
             return_value={"rsi": 72, "hour_utc": 12, "bb_squeeze": True, "vol_ratio": 1.6},
         ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            include_fundamentals=False,
            include_news=False,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="statistical",
            validated_setups_path=str(validated_path),
        )

    assert signal is not None
    assert signal["statistical_setup"] == "wide_long_rsi_above_70"


def test_generate_signal_statistical_does_not_force_asia_session_rejection(tmp_path):
    validated_path = tmp_path / "validated_setups.json"
    write_validated_export(validated_path)
    candles = make_trending_candles(120, "up")

    with patch.object(
        scanner,
        "_build_statistical_snapshot",
        return_value={"rsi": 72, "hour_utc": 2, "bb_squeeze": True, "vol_ratio": 1.6},
    ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            include_fundamentals=False,
            include_news=False,
            current_time=datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc),
            signal_model="statistical",
            validated_setups_path=str(validated_path),
        )

    assert signal is not None


def test_load_validated_setups_rejects_unknown_condition_names(tmp_path):
    validated_path = tmp_path / "broken_validated_setups.json"
    payload = {
        "validated_setups": {
            "long": [],
            "short": [
                {
                    "name": "broken_setup",
                    "template": "standard",
                    "direction": "SHORT",
                    "conditions": ["rsi_below_30", "does_not_exist"],
                    "tp_sl": {"sl_atr_mult": 1.5, "rr_ratio": 2.0},
                    "train_stats": {},
                    "test_stats": {},
                    "by_symbol": {},
                }
            ],
        }
    }
    validated_path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        scanner.load_validated_setups(str(validated_path))
    except ValueError as exc:
        assert "does_not_exist" in str(exc)
    else:
        raise AssertionError("Expected unknown setup condition to raise ValueError")


def test_run_backtest_raises_signal_generation_errors():
    candles = make_flat_candles_dict(4082, price=100.0)

    with patch.object(backtester, "fetch_klines_historical_cached", return_value=candles), \
         patch.object(backtester, "validate_candle_completeness", return_value=(True, "ok")), \
         patch.object(backtester.scanner, "precompute_indicators_for_all_candles", return_value={}), \
         patch.object(backtester, "generate_signal", side_effect=ValueError("bad setup file")):
        try:
            backtester.run_backtest(
                symbols=["BTCUSDT"],
                months=1,
                account=1000.0,
                fee_pct=0.0,
                slippage_pct=0.0,
                fixed_size=100.0,
                max_positions=1,
                cooldown_candles=1,
                use_next_open=True,
                kelly_sizing=False,
                signal_model="statistical",
                validated_setups_path="/tmp/validated.json",
            )
        except RuntimeError as exc:
            assert "generate_signal failed" in str(exc)
            assert "signal_model=statistical" in str(exc)
        else:
            raise AssertionError("Expected backtest to fail fast on generate_signal errors")


def test_generate_signal_technical_path_still_builds_tp_sl():
    candles = make_trending_candles(120, "up")
    mocked_tech = {
        "direction": "LONG",
        "score": 7.2,
        "long_score": 7.2,
        "short_score": 1.0,
        "details": {
            "strategy": "trend_pullback",
            "regime": "trending",
            "rsi": {"1h": 62},
            "adx": 28.0,
        },
    }

    with patch.object(scanner, "score_technical", return_value=mocked_tech):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            include_fundamentals=False,
            include_news=False,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="technical",
        )

    assert signal is not None
    assert signal["signal_model"] == "technical"
    assert signal["direction"] == "LONG"
    assert signal["tp"] > signal["entry_price"]
    assert signal["sl"] < signal["entry_price"]


def test_fetch_news_sentiment_uses_float_ratio_for_classification():
    headlines = ["BTC sees bullish setup with strong adoption and rally risk"]

    with patch.object(scanner, "fetch_rss_headlines", return_value=headlines), \
         patch.object(scanner, "filter_headlines_by_symbol", return_value=headlines):
        scanner._news_cache["by_symbol"]["BTCUSDT"] = {"ts": 0, "score": 0.0, "details": {}}
        score, details = scanner.fetch_news_sentiment("BTCUSDT")

    assert details["sentiment"] in {"bullish", "strong_bullish"}
    assert score >= 1.5


def test_repo_default_setup_artifacts_load_cleanly():
    validated = scanner.load_validated_setups(str(REPO_VALIDATED_SETUPS))
    curated = scanner.load_validated_setups(str(REPO_CURATED_SETUPS))

    assert validated["short"]
    assert curated["short"]


def test_repo_default_curated_signal_mode_generates_signal():
    candles = make_trending_candles(120, "down")
    snapshot = {
        "rsi": 25.0,
        "hour_utc": 15,
        "macd_line": -1.0,
        "macd_signal": -0.5,
        "macd_hist": -0.5,
        "macd_hist_prev": -0.2,
        "adx": 22.0,
        "vol_ratio": 1.0,
        "bb_squeeze": False,
        "e9": 98.0,
        "e21": 99.0,
        "e50": 100.0,
        "close": 97.0,
        "bb_upper": 102.0,
        "bb_lower": 94.0,
        "higher_highs": False,
        "lower_lows": True,
    }

    with patch.object(
        scanner,
        "_build_statistical_snapshot",
        return_value=snapshot,
    ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            include_fundamentals=False,
            include_news=False,
            current_time=datetime(2025, 1, 1, 15, 0, tzinfo=timezone.utc),
            signal_model="statistical_curated",
            validated_setups_path=str(REPO_CURATED_SETUPS),
        )

    assert signal is not None
    assert signal["signal_model"] == "statistical_curated"
    assert signal["direction"] == "SHORT"


def test_run_backtest_passes_statistical_signal_args():
    candles = make_flat_candles_dict(4082, price=100.0)
    signal = {
        "score": 6.5,
        "direction": "LONG",
        "strategy": "breakout",
        "regime": "breakout",
        "entry_price": 100.0,
        "tp": 110.0,
        "sl": 95.0,
        "tp_pct": 10.0,
        "sl_pct": 5.0,
        "atr": 1.0,
        "details": {"strategy": "breakout", "regime": "breakout"},
        "signal_model": "statistical",
    }

    with patch.object(backtester, "fetch_klines_historical_cached", return_value=candles), \
         patch.object(backtester, "validate_candle_completeness", return_value=(True, "ok")), \
         patch.object(backtester.scanner, "precompute_indicators_for_all_candles", return_value={}), \
         patch.object(backtester, "generate_signal", side_effect=[signal]) as mock_generate:
        backtester.run_backtest(
            symbols=["BTCUSDT"],
            months=1,
            account=1000.0,
            fee_pct=0.0,
            slippage_pct=0.0,
            fixed_size=100.0,
            max_positions=1,
            cooldown_candles=1,
            use_next_open=True,
            kelly_sizing=False,
            signal_model="statistical",
            validated_setups_path="/tmp/validated.json",
        )

    kwargs = mock_generate.call_args.kwargs
    assert kwargs["signal_model"] == "statistical"
    assert kwargs["validated_setups_path"] == "/tmp/validated.json"


def test_run_backtest_passes_curated_signal_args():
    candles = make_flat_candles_dict(4082, price=100.0)
    signal = {
        "score": 6.8,
        "direction": "SHORT",
        "strategy": "trend_pullback_weak",
        "regime": "weak_trend",
        "entry_price": 100.0,
        "tp": 95.0,
        "sl": 102.0,
        "tp_pct": 5.0,
        "sl_pct": 2.0,
        "atr": 1.0,
        "details": {"strategy": "trend_pullback_weak", "regime": "weak_trend"},
        "signal_model": "statistical_curated",
    }

    with patch.object(backtester, "fetch_klines_historical_cached", return_value=candles), \
         patch.object(backtester, "validate_candle_completeness", return_value=(True, "ok")), \
         patch.object(backtester.scanner, "precompute_indicators_for_all_candles", return_value={}), \
         patch.object(backtester, "generate_signal", side_effect=[signal]) as mock_generate:
        backtester.run_backtest(
            symbols=["AVAXUSDT"],
            months=1,
            account=1000.0,
            fee_pct=0.0,
            slippage_pct=0.0,
            fixed_size=100.0,
            max_positions=1,
            cooldown_candles=1,
            use_next_open=True,
            kelly_sizing=False,
            signal_model="statistical_curated",
            validated_setups_path="/tmp/curated.json",
        )

    kwargs = mock_generate.call_args.kwargs
    assert kwargs["signal_model"] == "statistical_curated"
    assert kwargs["validated_setups_path"] == "/tmp/curated.json"


def test_run_backtest_passes_dedicated_signal_mode_args():
    candles = make_flat_candles_dict(4082, price=100.0)
    signal = {
        "score": 0.0,
        "direction": "SHORT",
        "strategy": "statistical_wide_short_rsi28",
        "regime": "statistical",
        "entry_price": 100.0,
        "tp": 92.0,
        "sl": 103.0,
        "tp_pct": 8.0,
        "sl_pct": 3.0,
        "atr": 1.0,
        "details": {"strategy": "statistical_wide_short_rsi28", "regime": "statistical"},
        "signal_model": "statistical_wide_short_rsi28",
    }

    with patch.object(backtester, "fetch_klines_historical_cached", return_value=candles), \
         patch.object(backtester, "validate_candle_completeness", return_value=(True, "ok")), \
         patch.object(backtester, "generate_signal", side_effect=[signal]) as mock_generate:
        backtester.run_backtest(
            symbols=["BTCUSDT"],
            months=1,
            account=1000.0,
            fee_pct=0.0,
            slippage_pct=0.0,
            fixed_size=100.0,
            max_positions=1,
            cooldown_candles=1,
            use_next_open=True,
            kelly_sizing=False,
            signal_model="statistical_wide_short_rsi28",
        )

    kwargs = mock_generate.call_args.kwargs
    assert kwargs["signal_model"] == "statistical_wide_short_rsi28"


def test_run_backtest_passes_hybrid_signal_mode_args():
    candles = make_flat_candles_dict(4082, price=100.0)
    signal = {
        "score": 0.0,
        "direction": "SHORT",
        "strategy": "statistical_wide_short_rsi28",
        "regime": "statistical",
        "entry_price": 100.0,
        "tp": 92.0,
        "sl": 103.0,
        "tp_pct": 8.0,
        "sl_pct": 3.0,
        "atr": 1.0,
        "details": {"strategy": "statistical_wide_short_rsi28", "regime": "statistical"},
        "signal_model": "hybrid_technical_wide_short_rsi28",
        "hybrid_details": {"selected": {"source": "statistical"}},
    }

    with patch.object(backtester, "fetch_klines_historical_cached", return_value=candles), \
         patch.object(backtester, "validate_candle_completeness", return_value=(True, "ok")), \
         patch.object(backtester, "generate_signal", side_effect=[signal]) as mock_generate:
        backtester.run_backtest(
            symbols=["BTCUSDT"],
            months=1,
            account=1000.0,
            fee_pct=0.0,
            slippage_pct=0.0,
            fixed_size=100.0,
            max_positions=1,
            cooldown_candles=1,
            use_next_open=True,
            kelly_sizing=False,
            signal_model="hybrid_technical_wide_short_rsi28",
        )

    kwargs = mock_generate.call_args.kwargs
    assert kwargs["signal_model"] == "hybrid_technical_wide_short_rsi28"
