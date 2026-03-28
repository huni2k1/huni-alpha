"""
Pytest configuration and shared fixtures for trading bot tests.
"""
import pytest
import importlib.util
import os


@pytest.fixture(scope="session")
def market_scanner():
    """Load market-scanner.py module for all tests."""
    scanner_path = os.path.join(os.path.dirname(__file__), '..', 'scanner', 'market-scanner.py')
    spec = importlib.util.spec_from_file_location("market_scanner", scanner_path)
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    return scanner


@pytest.fixture
def trending_candles_up():
    """Generate 1000 candles in a clean uptrend."""
    candles = []
    price = 100.0
    for i in range(1000):
        price += 0.15
        candles.append([
            price - 0.05,   # open
            price + 0.10,   # high
            price - 0.10,   # low
            price,          # close
            1000.0 + (i % 50) * 20  # volume
        ])
    return candles


@pytest.fixture
def trending_candles_down():
    """Generate 1000 candles in a clean downtrend."""
    candles = []
    price = 100.0
    for i in range(1000):
        price -= 0.15
        candles.append([
            price - 0.05,   # open
            price + 0.10,   # high
            price - 0.10,   # low
            price,          # close
            1000.0 + (i % 50) * 20  # volume
        ])
    return candles


@pytest.fixture
def choppy_candles():
    """Generate 1000 candles in a sideways/choppy market."""
    import random
    random.seed(42)
    candles = []
    price = 100.0
    for i in range(1000):
        change = random.uniform(-0.3, 0.3)
        price = max(90.0, min(110.0, price + change))
        candles.append([
            price - 0.05,
            price + 0.15,
            price - 0.15,
            price,
            1000.0
        ])
    return candles


# ───────────────────────────────────────────────────────────
# Helpers for backtester tests (candle dicts, position dicts)
# ───────────────────────────────────────────────────────────

def make_candle_dict(open_, high, low, close, volume=1000, open_time_ms=None, close_time_ms=None):
    """Create a candle dict in backtester format (with open_time, close_time keys)."""
    if open_time_ms is None:
        open_time_ms = 1000000000000  # arbitrary timestamp
    if close_time_ms is None:
        close_time_ms = open_time_ms + 3600000  # 1 hour later
    return {
        "open_time": open_time_ms,
        "close_time": close_time_ms,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def make_position_dict(direction="LONG", entry_price=100.0, tp_price=110.0, sl_price=95.0,
                       position_size=100.0, atr=1.0, entry_idx=0, score=7.0, symbol="BTCUSDT"):
    """Create a position dict with all required keys for backtester operations."""
    return {
        "symbol": symbol,
        "direction": direction,
        "tier": "ENTRY",
        "score": score,
        "entry_price": entry_price,
        "entry_time": "2025-01-01T00:00:00Z",
        "entry_idx": entry_idx,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "current_sl": sl_price,
        "best_price": entry_price,
        "trail_activated": False,
        "max_favorable": 0.0,
        "max_adverse": 0.0,
        "position_size": position_size,
        "atr": atr,
        "tp_pct": abs((tp_price - entry_price) / entry_price * 100),
        "sl_pct": abs((entry_price - sl_price) / entry_price * 100),
        "signal": {"symbol": symbol, "direction": direction, "score": score},
        "partial_taken": False,
        "partial_pnl_usd": 0.0,
    }


def make_flat_candles_dict(n, price=100.0, volume=1000):
    """Create N flat candles (OHLCV dict format for backtester)."""
    candles = []
    for i in range(n):
        open_time_ms = 1000000000000 + (i * 3600000)  # 1 hour apart
        close_time_ms = open_time_ms + 3600000
        candles.append(make_candle_dict(
            open_=price,
            high=price + 0.5,
            low=price - 0.5,
            close=price,
            volume=volume,
            open_time_ms=open_time_ms,
            close_time_ms=close_time_ms,
        ))
    return candles
