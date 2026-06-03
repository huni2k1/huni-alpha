import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from trading_bot import backtester
from trading_bot import binance_http as _bhttp
from trading_bot import logging_setup as _lg
from trading_bot import signals as scanner
from trading_bot.core import indicators as _ind
from trading_bot.signals import engine as _sig_engine
from trading_bot.signals import rulebook as _sig_rulebook

REPO_VALIDATED_SETUPS = Path(scanner.RULEBOOK_PATH)
REPO_CURATED_SETUPS = Path(scanner.CURATED_RULEBOOK_PATH)


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


def write_long_crossover_scope_export(path):
    payload = {
        "schema_version": 1,
        "metadata": {},
        "validated_setups": {
            "long": [
                {
                    "name": "standard_long_ema50_cross_above_200_price_near_upper_bb_link",
                    "template": "standard",
                    "direction": "LONG",
                    "conditions": ["ema50_cross_above_200", "price_near_upper_bb"],
                    "tp_sl": {"sl_atr_mult": 1.5, "rr_ratio": 2.0},
                    "train_stats": {"count": 13, "win_rate": 69.2, "avg_pnl_pct": 3.6, "profit_factor": 2.4, "edge_win_rate": 20.0},
                    "test_stats": {"count": 13, "win_rate": 69.2, "avg_pnl_pct": 3.6, "profit_factor": 2.4, "edge_win_rate": 20.0},
                    "by_symbol": {"LINKUSDT": {"trades": 13, "win_rate": 69.2, "avg_pnl_pct": 3.6, "profit_factor": 2.4}},
                    "scope_key": "symbol_LINKUSDT",
                    "scope_type": "symbol",
                    "scope_symbol": "LINKUSDT",
                }
            ],
            "short": [],
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_rsi28_short_rulebook(path):
    payload = {
        "schema_version": 1,
        "metadata": {},
        "validated_setups": {
            "long": [],
            "short": [
                {
                    "name": "wide_short_rsi_below_28",
                    "template": "wide",
                    "direction": "SHORT",
                    "conditions": ["rsi_below_28"],
                    "tp_sl": {"sl_atr_mult": 2.0, "rr_ratio": 2.5},
                    "train_stats": {"count": 20, "win_rate": 60.0, "avg_pnl_pct": 1.0, "profit_factor": 1.8, "edge_win_rate": 20.0},
                    "test_stats": {"count": 10, "win_rate": 60.0, "avg_pnl_pct": 1.0, "profit_factor": 1.8, "edge_win_rate": 20.0},
                    "by_symbol": {},
                    "filter": {},
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
        _sig_engine,
        "_build_indicator_snapshot",
        return_value={"rsi": 72, "hour_utc": 12, "bb_squeeze": True, "vol_ratio": 1.6},
    ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="statistical",
            validated_setups_path=str(validated_path),
        )

    assert signal is not None
    assert signal["signal_engine"] == "rule_match"
    assert signal["direction"] == "LONG"
    assert signal["strategy"] == "rule_wide"
    assert signal["statistical_setup"] == "wide_long_rsi_above_70"
    assert signal["score"] == 1.3947


def test_generate_signal_statistical_returns_none_without_matching_setup(tmp_path):
    validated_path = tmp_path / "validated_setups.json"
    write_validated_export(validated_path)
    candles = make_trending_candles(120, "up")
    with patch.object(
        _sig_engine,
        "_build_indicator_snapshot",
        return_value={"rsi": 55, "hour_utc": 12, "bb_squeeze": True, "vol_ratio": 1.6},
    ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="statistical",
            validated_setups_path=str(validated_path),
        )

    assert signal is None


def test_generate_signal_rule_match_wide_short_rsi28_matches(tmp_path):
    rulebook_path = tmp_path / "rulebook.json"
    write_rsi28_short_rulebook(rulebook_path)
    candles = make_trending_candles(120, "down")
    with patch.object(
        _sig_engine,
        "_build_indicator_snapshot",
        return_value={"rsi": 27.5, "hour_utc": 12},
    ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_engine="rule_match",
            rulebook_path=str(rulebook_path),
        )

    assert signal is not None
    assert signal["signal_engine"] == "rule_match"
    assert signal["direction"] == "SHORT"
    assert signal["strategy"] == "rule_wide"
    assert signal["statistical_setup"] == "wide_short_rsi_below_28"


def test_generate_signal_rule_match_wide_short_rsi28_returns_none_without_match(tmp_path):
    rulebook_path = tmp_path / "rulebook.json"
    write_rsi28_short_rulebook(rulebook_path)
    candles = make_trending_candles(120, "down")
    with patch.object(
        _sig_engine,
        "_build_indicator_snapshot",
        return_value={"rsi": 29.0, "hour_utc": 12},
    ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_engine="rule_match",
            rulebook_path=str(rulebook_path),
        )

    assert signal is None


def test_generate_signal_combined_prefers_rule_match_short():
    candles = make_trending_candles(120, "down")
    ta_signal = {
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
        "long_score": 2.0,
        "short_score": 7.4,
        "regime": "breakout",
        "strategy": "breakout",
        "details": {"strategy": "breakout", "regime": "breakout"},
        "signal_engine": "ta_score",
    }
    rule_signal = {
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
        "long_score": 0.0,
        "short_score": 0.0,
        "regime": "rule_match",
        "strategy": "rule_wide",
        "details": {"strategy": "rule_wide", "regime": "rule_match"},
        "signal_engine": "rule_match",
        "statistical_setup": "wide_short_rsi_below_28",
        "statistical_details": {"conditions": ["rsi_below_28"], "template": "wide"},
    }

    with patch.object(_sig_engine, "_generate_ta_score_signal", return_value=ta_signal), \
         patch.object(_sig_engine, "_generate_rule_match_signal", return_value=rule_signal):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_engine="combined",
        )

    assert signal is not None
    assert signal["signal_engine"] == "combined"
    assert signal["strategy"] == "rule_wide"
    assert signal["hybrid_details"]["technical"]["strategy"] == "breakout"
    assert signal["hybrid_details"]["statistical"]["setup"] == "wide_short_rsi_below_28"
    assert signal["hybrid_details"]["selected"]["source"] == "statistical"


def test_generate_signal_combined_falls_back_to_ta_when_no_rule_fires():
    candles = make_trending_candles(120, "up")
    ta_signal = {
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
        "long_score": 7.6,
        "short_score": 2.0,
        "regime": "trending",
        "strategy": "trend_pullback",
        "details": {"strategy": "trend_pullback", "regime": "trending"},
        "signal_engine": "ta_score",
    }

    with patch.object(_sig_engine, "_generate_ta_score_signal", return_value=ta_signal), \
         patch.object(_sig_engine, "_generate_rule_match_signal", return_value=None):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_engine="combined",
        )

    assert signal is not None
    assert signal["direction"] == "LONG"
    assert signal["strategy"] == "trend_pullback"
    assert signal["hybrid_details"]["selected"]["source"] == "technical"


def test_generate_signal_combined_reports_rejection_when_no_signal():
    candles = make_trending_candles(120, "up")

    with patch.object(_sig_engine, "_generate_ta_score_signal", return_value=None), \
         patch.object(_sig_engine, "_generate_rule_match_signal", return_value=None):
        scanner._last_rejection_reason["BTCUSDT"] = "No validated setup"
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_engine="combined",
        )

    assert signal is None
    assert "Hybrid rejected" in scanner._last_rejection_reason["BTCUSDT"]


def test_generate_signal_statistical_curated_prefers_scoped_setup(tmp_path):
    validated_path = tmp_path / "curated_setups.json"
    write_curated_scope_export(validated_path)
    candles = make_trending_candles(120, "down")
    with patch.object(
        _sig_engine,
        "_build_indicator_snapshot",
        return_value={"rsi": 35, "adx": 21, "bb_squeeze": False, "vol_ratio": 1.0, "hour_utc": 12},
    ):
        signal = scanner.generate_signal(
            "AVAXUSDT",
            candles,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="statistical_curated",
            validated_setups_path=str(validated_path),
        )

    assert signal is not None
    assert signal["signal_engine"] == "rule_match"
    assert signal["direction"] == "SHORT"
    assert signal["statistical_setup"] == "standard_short_rsi_below_40_avax_weak"
    assert signal["statistical_details"]["filter"]["symbol"] == "AVAXUSDT"
    assert signal["statistical_details"]["filter"]["regime"] is None


def test_generate_signal_statistical_curated_matches_new_long_crossover_setup(tmp_path):
    validated_path = tmp_path / "long_crossover_setups.json"
    write_long_crossover_scope_export(validated_path)
    candles = make_trending_candles(220, "up")
    with patch.object(
        _sig_engine,
        "_build_indicator_snapshot",
        return_value={
            "rsi": 64,
            "adx": 28,
            "bb_squeeze": False,
            "vol_ratio": 1.1,
            "hour_utc": 12,
            "close": 100.0,
            "bb_upper": 100.5,
            "e50": 101.0,
            "e200": 100.0,
            "e50_prev": 99.0,
            "e200_prev": 100.0,
        },
    ):
        signal = scanner.generate_signal(
            "LINKUSDT",
            candles,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="statistical_curated",
            validated_setups_path=str(validated_path),
        )

    assert signal is not None
    assert signal["signal_engine"] == "rule_match"
    assert signal["direction"] == "LONG"
    assert signal["statistical_setup"] == "standard_long_ema50_cross_above_200_price_near_upper_bb_link"


def test_load_validated_setups_preserves_indicator_only_rules(tmp_path):
    curated_path = tmp_path / "curated_setups.json"
    validated_path = tmp_path / "validated_setups.json"
    write_curated_scope_export(curated_path)
    write_validated_export(validated_path)

    curated = scanner.load_validated_setups(str(curated_path))
    validated = scanner.load_validated_setups(str(validated_path))
    avax_setup = next(setup for setup in curated["short"] if setup["filter"]["symbol"] == "AVAXUSDT")
    pooled_trend_setup = next(setup for setup in curated["short"] if not setup["filter"]["symbol"])
    breakout_setup = validated["short"][0]

    assert "adx_below_25" in avax_setup["conditions"]
    assert avax_setup["filter"]["symbol"] == "AVAXUSDT"
    assert avax_setup["filter"]["regime"] is None

    assert "bb_squeeze" not in avax_setup["conditions"]
    assert "rsi_below_40" in pooled_trend_setup["conditions"]
    assert "bb_squeeze" in breakout_setup["conditions"]
    assert "vol_above_1_5" in breakout_setup["conditions"]


def test_generate_signal_statistical_ignores_technical_neutral_gate(tmp_path):
    validated_path = tmp_path / "validated_setups.json"
    write_validated_export(validated_path)
    candles = make_trending_candles(120, "up")

    with patch.object(_sig_engine, "score_technical", return_value={"direction": "NEUTRAL"}), \
         patch.object(
             _sig_engine,
             "_build_indicator_snapshot",
             return_value={"rsi": 72, "hour_utc": 12, "bb_squeeze": True, "vol_ratio": 1.6},
         ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
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
        _sig_engine,
        "_build_indicator_snapshot",
        return_value={"rsi": 72, "hour_utc": 2, "bb_squeeze": True, "vol_ratio": 1.6},
    ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
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
         patch.object(backtester, "precompute_indicators_for_all_candles", return_value={}), \
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
                signal_engine="rule_match",
                rulebook_path="/tmp/validated.json",
            )
        except RuntimeError as exc:
            assert "generate_signal failed" in str(exc)
            assert "signal_engine=rule_match" in str(exc)
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

    with patch.object(_sig_engine, "score_technical", return_value=mocked_tech):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            current_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            signal_model="technical",
        )

    assert signal is not None
    assert signal["signal_engine"] == "ta_score"
    assert signal["direction"] == "LONG"
    assert signal["tp"] > signal["entry_price"]
    assert signal["sl"] < signal["entry_price"]


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
        _sig_engine,
        "_build_indicator_snapshot",
        return_value=snapshot,
    ):
        signal = scanner.generate_signal(
            "BTCUSDT",
            candles,
            current_time=datetime(2025, 1, 1, 15, 0, tzinfo=timezone.utc),
            signal_model="statistical_curated",
            validated_setups_path=str(REPO_CURATED_SETUPS),
        )

    assert signal is not None
    assert signal["signal_engine"] == "rule_match"
    assert signal["direction"] == "SHORT"


def test_run_backtest_passes_rule_match_signal_args():
    candles = make_flat_candles_dict(4082, price=100.0)
    signal = {
        "score": 6.5,
        "direction": "LONG",
        "strategy": "rule_wide",
        "regime": "rule_match",
        "entry_price": 100.0,
        "tp": 110.0,
        "sl": 95.0,
        "tp_pct": 10.0,
        "sl_pct": 5.0,
        "atr": 1.0,
        "details": {"strategy": "rule_wide", "regime": "rule_match"},
        "signal_engine": "rule_match",
    }

    with patch.object(backtester, "fetch_klines_historical_cached", return_value=candles), \
         patch.object(backtester, "validate_candle_completeness", return_value=(True, "ok")), \
         patch.object(backtester, "precompute_indicators_for_all_candles", return_value={}), \
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
            signal_engine="rule_match",
            rulebook_path="/tmp/validated.json",
        )

    kwargs = mock_generate.call_args.kwargs
    assert kwargs["signal_engine"] == "rule_match"
    assert kwargs["rulebook_path"] == "/tmp/validated.json"


def test_run_backtest_passes_combined_signal_args():
    candles = make_flat_candles_dict(4082, price=100.0)
    signal = {
        "score": 0.0,
        "direction": "SHORT",
        "strategy": "rule_wide",
        "regime": "rule_match",
        "entry_price": 100.0,
        "tp": 92.0,
        "sl": 103.0,
        "tp_pct": 8.0,
        "sl_pct": 3.0,
        "atr": 1.0,
        "details": {"strategy": "rule_wide", "regime": "rule_match"},
        "signal_engine": "combined",
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
            signal_engine="combined",
        )

    kwargs = mock_generate.call_args.kwargs
    assert kwargs["signal_engine"] == "combined"
