"""Unit tests for execution.fills.resolve_same_candle_hit.

Algorithm: when both TP and SL are hit in the same candle, return whichever
level is CLOSER to the candle's open price (less distance to travel from the
observable starting point).
"""

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_bot.execution.fills import resolve_same_candle_hit


def _candle(open_price: float) -> dict:
    return {"open": open_price, "high": open_price, "low": open_price, "close": open_price}


# ── Open closer to TP ────────────────────────────────────────────────────────

def test_open_closer_to_tp_returns_tp_long():
    # open=108, TP=110, SL=95 → dist 2 vs 13 → TP
    assert resolve_same_candle_hit(_candle(108.0), tp_price=110.0, sl_price=95.0) == "TP"


def test_open_closer_to_tp_returns_tp_short():
    # open=92, TP=90, SL=105 → dist 2 vs 13 → TP
    assert resolve_same_candle_hit(_candle(92.0), tp_price=90.0, sl_price=105.0) == "TP"


# ── Open closer to SL ────────────────────────────────────────────────────────

def test_open_closer_to_sl_returns_sl_long():
    # open=97, TP=110, SL=95 → dist 13 vs 2 → SL
    assert resolve_same_candle_hit(_candle(97.0), tp_price=110.0, sl_price=95.0) == "SL"


def test_open_closer_to_sl_returns_sl_short():
    # open=103, TP=90, SL=105 → dist 13 vs 2 → SL
    assert resolve_same_candle_hit(_candle(103.0), tp_price=90.0, sl_price=105.0) == "SL"


# ── Gap cases (open past one level) ──────────────────────────────────────────

def test_open_gapped_past_tp_long():
    # open=112 above TP=110 → TP closer
    assert resolve_same_candle_hit(_candle(112.0), tp_price=110.0, sl_price=95.0) == "TP"


def test_open_gapped_past_sl_long():
    # open=93 below SL=95 → SL closer
    assert resolve_same_candle_hit(_candle(93.0), tp_price=110.0, sl_price=95.0) == "SL"


# ── Tie-break: "sl" (deterministic, research) ────────────────────────────────

def test_tie_break_sl_is_deterministic_at_midpoint():
    # open=102.5 is exactly midway between SL=95 and TP=110 → equidistant
    for _ in range(20):
        result = resolve_same_candle_hit(
            _candle(102.5), tp_price=110.0, sl_price=95.0, tie_break="sl"
        )
        assert result == "SL"


# ── Tie-break: "random" (live/backtest realism) ──────────────────────────────

def test_tie_break_random_can_return_either_at_midpoint():
    random.seed(0)
    outcomes = {
        resolve_same_candle_hit(_candle(102.5), tp_price=110.0, sl_price=95.0)
        for _ in range(40)
    }
    assert outcomes == {"TP", "SL"}


# ── Type coercion ────────────────────────────────────────────────────────────

def test_accepts_string_numeric_prices():
    assert resolve_same_candle_hit({"open": "108"}, tp_price="110", sl_price="95") == "TP"
