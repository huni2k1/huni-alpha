"""
Regression tests for backtester cache behavior.

These tests ensure the backtester cache preserves real exchange timestamps
and does not collide with the scanner's simpler OHLCV cache format.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from trading_bot import backtester as bt


def make_candle_dict(open_time_ms: int, price: float) -> dict:
    """Create a simple 1h candle dict for cache tests."""
    return {
        "open_time": open_time_ms,
        "open": price,
        "high": price + 1.0,
        "low": price - 1.0,
        "close": price + 0.25,
        "volume": 1000.0,
        "close_time": open_time_ms + 3_600_000 - 1,
    }


def test_backtester_cache_hit_preserves_real_timestamps(tmp_path, monkeypatch):
    """Cache hits must preserve real open_time values, including gaps."""
    monkeypatch.setattr(bt.candle_cache, "CACHE_DIR", str(tmp_path))

    symbol = "BTCUSDT"
    interval = "1h"
    start_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=1)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")

    candles = []
    for hour in range(24):
        if hour == 5:
            continue  # Preserve a real 2h hole in the exchange timeline.
        open_time_ms = int((start_dt + timedelta(hours=hour)).timestamp() * 1000)
        candles.append(make_candle_dict(open_time_ms, 100.0 + hour))

    assert bt.candle_cache.save_to_cache(
        symbol,
        interval,
        start_str,
        end_str,
        candles,
        variant=bt.BACKTESTER_CACHE_VARIANT,
    )

    with patch.object(bt, "fetch_klines_historical", side_effect=AssertionError("unexpected refetch")):
        result = bt.fetch_klines_historical_cached(symbol, interval, start_ms, end_ms, use_cache=True)

    assert result == candles
    assert result[5]["open_time"] - result[4]["open_time"] == 2 * 3_600_000


def test_cache_variants_keep_scanner_and_backtester_formats_separate(tmp_path, monkeypatch):
    """Backtester timestamped candles should not overwrite the scanner OHLCV cache."""
    monkeypatch.setattr(bt.candle_cache, "CACHE_DIR", str(tmp_path))

    symbol = "BTCUSDT"
    interval = "1h"
    start_dt = datetime(2026, 2, 1, tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=1)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")

    scanner_candles = []
    backtester_candles = []
    for hour in range(24):
        open_time_ms = int((start_dt + timedelta(hours=hour)).timestamp() * 1000)
        price = 200.0 + hour
        scanner_candles.append([price, price + 1.0, price - 1.0, price + 0.25, 1000.0])
        backtester_candles.append(make_candle_dict(open_time_ms, price))

    assert bt.candle_cache.save_to_cache(symbol, interval, start_str, end_str, scanner_candles)
    assert bt.candle_cache.save_to_cache(
        symbol,
        interval,
        start_str,
        end_str,
        backtester_candles,
        variant=bt.BACKTESTER_CACHE_VARIANT,
    )

    assert bt.candle_cache.load_from_cache(symbol, interval, start_str, end_str) == scanner_candles
    assert bt.candle_cache.load_from_cache(
        symbol,
        interval,
        start_str,
        end_str,
        variant=bt.BACKTESTER_CACHE_VARIANT,
    ) == backtester_candles
