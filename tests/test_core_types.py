"""Unit tests for core.types.Candle.

The Literal aliases (Direction, MarketRegime, …) are pure type hints — they
have no runtime behavior to test. Only Candle has runtime conversions.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_bot.core.types import Candle, Signal


# ── construction ─────────────────────────────────────────────────────────────

def test_candle_minimal_construction():
    c = Candle(open=100.0, high=101.0, low=99.0, close=100.5, volume=10.0)
    assert c.open == 100.0
    assert c.high == 101.0
    assert c.low == 99.0
    assert c.close == 100.5
    assert c.volume == 10.0
    assert c.open_time is None
    assert c.close_time is None
    assert c.symbol is None


def test_candle_is_frozen():
    c = Candle(open=100.0, high=101.0, low=99.0, close=100.5, volume=10.0)
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        c.open = 999.0  # type: ignore[misc]


def test_candle_is_hashable():
    c1 = Candle(open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)
    c2 = Candle(open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)
    assert hash(c1) == hash(c2)
    assert {c1, c2} == {c1}


# ── from_dict ────────────────────────────────────────────────────────────────

def test_from_dict_basic():
    d = {
        "open": "100.5",  # strings should coerce
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "volume": "1000",
        "open_time": 1700000000000,
        "close_time": 1700003599999,
        "symbol": "BTCUSDT",
    }
    c = Candle.from_dict(d)
    assert c.open == 100.5
    assert c.volume == 1000.0
    assert c.symbol == "BTCUSDT"
    assert c.open_time == 1700000000000


def test_from_dict_handles_missing_optional_fields():
    d = {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}
    c = Candle.from_dict(d)
    assert c.volume == 0.0
    assert c.open_time is None
    assert c.close_time is None
    assert c.symbol is None


# ── from_ohlcv_list ──────────────────────────────────────────────────────────

def test_from_ohlcv_list_binance_format():
    # Binance kline: [open, high, low, close, volume, ...]
    values = ["100.5", "101.0", "99.0", "100.0", "1000", "ignored"]
    c = Candle.from_ohlcv_list(values, symbol="BTCUSDT")
    assert c.open == 100.5
    assert c.close == 100.0
    assert c.volume == 1000.0
    assert c.symbol == "BTCUSDT"


def test_from_ohlcv_list_minimum_4_fields():
    # No volume column → defaults to 0
    c = Candle.from_ohlcv_list([100.0, 101.0, 99.0, 100.5])
    assert c.volume == 0.0


# ── round-trip ───────────────────────────────────────────────────────────────

def test_round_trip_via_dict():
    original = Candle(
        open=100.0, high=101.0, low=99.0, close=100.5, volume=10.0,
        open_time=1700000000000, close_time=1700003599999, symbol="ETHUSDT",
    )
    restored = Candle.from_dict(original.to_dict())
    assert restored == original


# ─────────────────────────────────────────────────────────────────
# Signal
# ─────────────────────────────────────────────────────────────────

def _minimal_signal_dict():
    return {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "score": 7.5,
        "entry_price": 100.0,
        "tp": 110.0,
        "sl": 95.0,
        "tp_pct": 10.0,
        "sl_pct": 5.0,
        "atr": 2.0,
        "sl_atr_mult": 2.5,
        "rr_ratio": 2.0,
    }


def test_signal_minimal_construction():
    s = Signal(
        symbol="BTCUSDT", direction="LONG", score=7.5,
        entry_price=100.0, tp=110.0, sl=95.0, tp_pct=10.0, sl_pct=5.0,
        atr=2.0, sl_atr_mult=2.5, rr_ratio=2.0,
    )
    assert s.symbol == "BTCUSDT"
    assert s.direction == "LONG"
    # Defaults
    assert s.signal_engine == "ta_score"
    assert s.strategy == "trend_pullback"
    assert s.long_score == 0.0
    assert s.details == {}
    assert s.statistical_setup is None
    assert s.hybrid_details is None


def test_signal_is_frozen():
    s = Signal(
        symbol="BTCUSDT", direction="LONG", score=7.5,
        entry_price=100.0, tp=110.0, sl=95.0, tp_pct=10.0, sl_pct=5.0,
        atr=2.0, sl_atr_mult=2.5, rr_ratio=2.0,
    )
    with pytest.raises(Exception):
        s.score = 999.0  # type: ignore[misc]


def test_signal_from_dict_minimal():
    s = Signal.from_dict(_minimal_signal_dict())
    assert s.direction == "LONG"
    assert s.tp == 110.0
    assert s.signal_engine == "ta_score"  # default applied


def test_signal_from_dict_rule_match():
    d = _minimal_signal_dict()
    d.update({
        "signal_engine": "rule_match",
        "strategy": "rule_wide",
        "regime": "rule_match",
        "statistical_setup": "wide_short_rsi_below_32",
        "statistical_details": {"matched_rule": "wide_short_rsi_below_32"},
    })
    s = Signal.from_dict(d)
    assert s.signal_engine == "rule_match"
    assert s.statistical_setup == "wide_short_rsi_below_32"
    assert s.statistical_details["matched_rule"] == "wide_short_rsi_below_32"
    assert s.hybrid_details is None


def test_signal_from_dict_combined_with_hybrid_details():
    d = _minimal_signal_dict()
    d.update({
        "signal_engine": "combined",
        "hybrid_details": {"selected": {"source": "statistical"}},
    })
    s = Signal.from_dict(d)
    assert s.signal_engine == "combined"
    assert s.hybrid_details["selected"]["source"] == "statistical"


def test_signal_from_dict_coerces_numeric_strings():
    d = _minimal_signal_dict()
    d["score"] = "7.5"
    d["atr"] = "2.0"
    s = Signal.from_dict(d)
    assert s.score == 7.5
    assert s.atr == 2.0


def test_signal_from_dict_handles_none_for_recipe_fields():
    # Old signals may carry explicit None for atr/rr_ratio — should coerce to defaults.
    d = _minimal_signal_dict()
    d["atr"] = None
    d["sl_atr_mult"] = None
    s = Signal.from_dict(d)
    assert s.atr == 0.0
    assert s.sl_atr_mult == 1.5


def test_signal_to_dict_writes_legacy_signal_model_alias():
    s = Signal.from_dict({**_minimal_signal_dict(), "signal_engine": "combined"})
    d = s.to_dict()
    assert d["signal_engine"] == "combined"
    assert d["signal_model"] == "combined"   # legacy consumers


def test_signal_round_trip_via_dict():
    src = _minimal_signal_dict()
    src.update({"signal_engine": "rule_match", "statistical_setup": "foo"})
    s = Signal.from_dict(src)
    restored = Signal.from_dict(s.to_dict())
    assert restored == s
