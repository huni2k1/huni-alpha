"""Unit tests for execution.gating.

Pins the three post-signal gate functions so live and backtest can't drift.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_bot.execution.gating import (
    at_max_positions,
    in_cooldown,
    required_threshold,
)


# ── required_threshold ───────────────────────────────────────────────────────

def test_rule_match_engine_returns_none():
    assert required_threshold("rule_match", "rule_wide", 7.0, 6.0) is None


def test_combined_engine_rule_leg_bypasses_threshold():
    # strategy contains "rule_" → rule leg → no threshold
    assert required_threshold("combined", "rule_wide", 7.0, 6.0) is None
    assert required_threshold("combined", "rule_standard", 7.0, 6.0) is None


def test_combined_engine_statistical_in_strategy_bypasses():
    assert required_threshold("combined", "statistical_wide_short", 7.0, 6.0) is None


def test_combined_engine_technical_leg_uses_threshold():
    # No rule_ / statistical → technical fallback → threshold applies
    assert required_threshold("combined", "trend_pullback", 7.0, 6.0) == 7.0
    assert required_threshold("combined", "breakout", 7.0, 6.0) == 6.0


def test_ta_score_engine_trend_uses_trend_threshold():
    assert required_threshold("ta_score", "trend_pullback", 7.0, 6.0) == 7.0


def test_ta_score_engine_breakout_uses_breakout_threshold():
    assert required_threshold("ta_score", "breakout", 7.0, 6.0) == 6.0


def test_thresholds_are_caller_supplied_not_hardcoded():
    # Passing different thresholds → different output
    assert required_threshold("ta_score", "trend_pullback", 8.5, 6.0) == 8.5
    assert required_threshold("ta_score", "breakout", 7.0, 5.0) == 5.0


# ── in_cooldown ──────────────────────────────────────────────────────────────

def test_cooldown_blocks_when_elapsed_less_than_required():
    assert in_cooldown(elapsed=10, required=20) is True


def test_cooldown_allows_when_elapsed_equals_required():
    assert in_cooldown(elapsed=20, required=20) is False


def test_cooldown_allows_when_elapsed_exceeds_required():
    assert in_cooldown(elapsed=30, required=20) is False


def test_cooldown_zero_required_is_disabled():
    # required <= 0 means cooldown is off
    assert in_cooldown(elapsed=0, required=0) is False


def test_cooldown_negative_elapsed_is_safe():
    # Never-fired sentinel (e.g. backtester's -999) → not in cooldown
    assert in_cooldown(elapsed=-999, required=48) is False


# ── at_max_positions ─────────────────────────────────────────────────────────

def test_at_max_returns_false_when_room_remains():
    assert at_max_positions(0, 3) is False
    assert at_max_positions(2, 3) is False


def test_at_max_returns_true_when_full():
    assert at_max_positions(3, 3) is True


def test_at_max_returns_true_when_overfull():
    # Shouldn't happen in practice, but be defensive
    assert at_max_positions(5, 3) is True
