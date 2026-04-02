import importlib.util
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
backtester_path = os.path.join(os.path.dirname(__file__), "..", "src", "trading_bot", "backtester.py")
spec = importlib.util.spec_from_file_location("backtester", backtester_path)
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)


def make_candle_dict(open_, high, low, close, volume=1000, open_time_ms=None, close_time_ms=None):
    if open_time_ms is None:
        open_time_ms = 1000000000000
    if close_time_ms is None:
        close_time_ms = open_time_ms + 3600000
    return {
        "open_time": open_time_ms,
        "close_time": close_time_ms,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def make_flat_candles_dict(n, price=100.0, volume=1000):
    candles = []
    for i in range(n):
        open_time_ms = 1000000000000 + (i * 3600000)
        close_time_ms = open_time_ms + 3600000 - 1
        candles.append(
            make_candle_dict(
                open_=price,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                volume=volume,
                open_time_ms=open_time_ms,
                close_time_ms=close_time_ms,
            )
        )
    return candles


def test_next_open_entry_uses_actual_entry_candle_time_and_index():
    candles = make_flat_candles_dict(4082, price=100.0)
    entry_idx = 4081
    candles[entry_idx]["open"] = 123.0
    candles[entry_idx]["high"] = 123.5
    candles[entry_idx]["low"] = 122.5
    candles[entry_idx]["close"] = 123.0

    signal = {
        "score": 7.0,
        "direction": "LONG",
        "strategy": "trend_pullback",
        "regime": "trending",
        "entry_price": 100.0,
        "tp": 110.0,
        "sl": 95.0,
        "tp_pct": 10.0,
        "sl_pct": 5.0,
        "atr": 1.0,
        "details": {"strategy": "trend_pullback", "regime": "trending"},
    }

    with patch.object(bt, "fetch_klines_historical_cached", return_value=candles), \
         patch.object(bt, "validate_candle_completeness", return_value=(True, "ok")), \
         patch.object(bt.scanner, "precompute_indicators_for_all_candles", return_value={}), \
         patch.object(bt, "generate_signal", side_effect=[signal]):
        results = bt.run_backtest(
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
        )

    assert len(results["trades"]) == 1

    trade = results["trades"][0]
    expected_entry_time = datetime.fromtimestamp(
        candles[entry_idx]["open_time"] / 1000, tz=timezone.utc
    ).isoformat()

    assert trade["entry_price"] == candles[entry_idx]["open"]
    assert trade["entry_time"] == expected_entry_time
    assert trade["duration_hours"] == 0


def test_next_open_reanchors_tp_sl_and_allows_position_size_above_old_hard_cap():
    candles = make_flat_candles_dict(4082, price=100.0)
    entry_idx = 4081
    candles[entry_idx]["open"] = 120.0
    candles[entry_idx]["high"] = 120.5
    candles[entry_idx]["low"] = 119.5
    candles[entry_idx]["close"] = 120.0

    signal = {
        "score": 7.0,
        "direction": "LONG",
        "strategy": "trend_pullback",
        "regime": "trending",
        "entry_price": 100.0,
        "tp": 110.0,
        "sl": 99.0,
        "tp_pct": 10.0,
        "sl_pct": 1.0,
        "atr": 1.0,
        "details": {"strategy": "trend_pullback", "regime": "trending", "rsi": {}},
    }

    with patch.object(bt, "fetch_klines_historical_cached", return_value=candles), \
         patch.object(bt, "validate_candle_completeness", return_value=(True, "ok")), \
         patch.object(bt, "generate_signal", side_effect=[signal]):
        results = bt.run_backtest(
            symbols=["BTCUSDT"],
            months=1,
            account=5000.0,
            fee_pct=0.0,
            slippage_pct=0.0,
            fixed_size=0.0,
            max_positions=1,
            cooldown_candles=1,
            use_next_open=True,
            kelly_sizing=False,
        )

    assert len(results["trades"]) == 1

    trade = results["trades"][0]
    assert trade["entry_price"] == 120.0
    assert trade["tp_price"] == 130.0
    assert trade["sl_price"] == 119.0
    assert trade["position_size"] == pytest.approx(2500.0, abs=0.01)
