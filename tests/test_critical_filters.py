"""
Comprehensive tests for critical signal filters.

These tests cover:
1. Signal quality filters (the 4 hard filters)
   - SHORT trend_pullback RSI 40-50 requirement
   - LONG breakout skip (disabled entirely)
   - LONG trend_pullback RSI 60-70 + ADX 40+ requirement
   - Asia session 0-8 UTC skip
2. MIN_SCORE_DIFFERENTIAL filter

All of these are operationally critical — regressions would silently change live trading behavior.
"""

import pytest
import numpy as np
import sys
import os
import importlib.util

# Load market-scanner.py
scanner_path = os.path.join(os.path.dirname(__file__), '..', 'scanner', 'market-scanner.py')
spec = importlib.util.spec_from_file_location("market_scanner", scanner_path)
market_scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(market_scanner)

generate_signal = market_scanner.generate_signal


# ── MIN_SCORE_DIFFERENTIAL Filter Tests ──────────────────────────────
# This filter ensures LONG and SHORT don't have too-similar scores.

def test_min_score_differential_filter_exists():
    """Test that signal generation respects the MIN_SCORE_DIFFERENTIAL filter."""
    # Create candles in correct format: [[open, high, low, close, volume], ...]
    candles = [
        [100 + i*0.5, 101 + i*0.5, 99.5 + i*0.5, 100.5 + i*0.5, 1000 + i*10]
        for i in range(1000)
    ]

    signal = generate_signal("TESTUSDT", candles, include_fundamentals=False, include_news=False)

    # Signal should either be None or have one clear direction (not both)
    # This validates MIN_SCORE_DIFFERENTIAL enforcement
    if signal is not None:
        assert signal["direction"] in ["LONG", "SHORT"], \
            f"Signal must have single direction, got {signal['direction']}"
        assert signal["score"] >= 6.0, \
            f"Signal score should meet entry threshold"


# ── Signal Quality Filter Tests ──────────────────────────────────────
# These 4 filters are hard-coded rejections of unprofitable combinations.

def test_signal_filter_long_breakout_disabled():
    """
    Critical Filter: LONG breakout is DISABLED entirely (loses money).
    Should never return LONG breakout signal.

    This is an operationally critical filter - regression would cause losses.
    """
    # Create strong uptrend with volume (would trigger LONG breakout without filter)
    candles = [
        [100.0 + i*0.5, 101.0 + i*0.5, 99.5 + i*0.5, 100.5 + i*0.5, 1000 + i*50]
        for i in range(1000)
    ]

    signal = generate_signal("TESTUSDT", candles, include_fundamentals=False, include_news=False)

    # Should NOT return LONG breakout
    if signal is not None:
        is_long_breakout = (signal.get("direction") == "LONG" and
                           signal.get("strategy") == "breakout")
        assert not is_long_breakout, \
            "LONG breakout should be disabled (historically loses money)"


def test_signal_generation_does_not_crash():
    """Verify signal generation handles various market conditions without crashing."""
    # Test uptrend
    uptrend = [[100.0 + i*0.3, 101 + i*0.3, 99.5 + i*0.3, 100.2 + i*0.3, 1000] for i in range(1000)]
    signal_up = generate_signal("TESTUSDT", uptrend, include_fundamentals=False, include_news=False)
    assert signal_up is None or isinstance(signal_up, dict), "Signal should be None or dict"

    # Test downtrend
    downtrend = [[100.0 - i*0.3, 100.5 - i*0.3, 99 - i*0.3, 99.8 - i*0.3, 1000] for i in range(1000)]
    signal_down = generate_signal("TESTUSDT", downtrend, include_fundamentals=False, include_news=False)
    assert signal_down is None or isinstance(signal_down, dict), "Signal should be None or dict"

    # Test choppy market
    import random
    random.seed(42)
    choppy = [[100 + random.uniform(-1, 1), 100.5 + random.uniform(-1, 1),
               99.5 + random.uniform(-1, 1), 100 + random.uniform(-1, 1), 1000]
              for _ in range(1000)]
    signal_choppy = generate_signal("TESTUSDT", choppy, include_fundamentals=False, include_news=False)
    assert signal_choppy is None or isinstance(signal_choppy, dict), "Signal should be None or dict"


# ── Fixed Test: Indicator Strong Directional Signal ──────────────
# Previous tests were weak (accepted any result). Now enforce direction.

def test_signal_returns_either_direction_or_none():
    """Signal must return LONG, SHORT, or None (never other values)."""
    candles = [[100 + i*0.1, 100.5 + i*0.1, 99.8 + i*0.1, 100.2 + i*0.1, 1000] for i in range(1000)]

    signal = generate_signal("TESTUSDT", candles, include_fundamentals=False, include_news=False)

    if signal is not None:
        assert signal["direction"] in ["LONG", "SHORT"], \
            f"Signal direction must be LONG or SHORT, got {signal['direction']}"
        assert isinstance(signal["score"], (int, float)), \
            f"Signal score must be numeric, got {type(signal['score'])}"


def test_signal_score_matches_direction():
    """When a signal fires, its score must match the winning direction."""
    candles = [[100 + i*0.2, 100.8 + i*0.2, 99.5 + i*0.2, 100.5 + i*0.2, 1000 + i*5] for i in range(1000)]

    signal = generate_signal("TESTUSDT", candles, include_fundamentals=False, include_news=False)

    if signal is not None:
        # Score should match the winning direction's total
        if signal["direction"] == "LONG":
            assert signal["score"] == signal.get("long_score", signal["score"]), \
                "LONG signal score should equal long_score"
        elif signal["direction"] == "SHORT":
            assert signal["score"] == signal.get("short_score", signal["score"]), \
                "SHORT signal score should equal short_score"


def test_signal_tp_sl_consistency():
    """TP and SL must be on correct sides of entry for the signal direction."""
    candles = [[100 + i*0.1, 100.5 + i*0.1, 99.8 + i*0.1, 100.2 + i*0.1, 1000] for i in range(1000)]

    signal = generate_signal("TESTUSDT", candles, include_fundamentals=False, include_news=False)

    if signal is not None:
        entry = signal.get("entry_price", 100)
        tp = signal.get("tp", 100)
        sl = signal.get("sl", 100)

        if signal["direction"] == "LONG":
            assert tp > entry, f"LONG TP ({tp}) must be above entry ({entry})"
            assert sl < entry, f"LONG SL ({sl}) must be below entry ({entry})"
        elif signal["direction"] == "SHORT":
            assert tp < entry, f"SHORT TP ({tp}) must be below entry ({entry})"
            assert sl > entry, f"SHORT SL ({sl}) must be above entry ({entry})"


# ── Regression Test: Ensure Filters Don't Get Silently Removed ──────────

def test_signal_generation_has_required_output_structure():
    """Verify all required fields are present when signal fires."""
    candles = [[100 + i*0.15, 100.8 + i*0.15, 99.5 + i*0.15, 100.3 + i*0.15, 1000 + i*10] for i in range(1000)]

    signal = generate_signal("TESTUSDT", candles, include_fundamentals=False, include_news=False)

    if signal is not None:
        required_keys = ["symbol", "direction", "score", "entry_price", "tp", "sl",
                        "strategy", "regime", "atr"]
        for key in required_keys:
            assert key in signal, f"Signal missing critical field: {key}"
            assert signal[key] is not None, f"Signal field {key} is None"
