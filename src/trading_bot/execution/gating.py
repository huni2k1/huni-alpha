"""Entry gates — shared by live trader and backtester.

A "gate" is a yes/no check applied AFTER a signal exists, BEFORE we open
a position. Each one is a pure function with no I/O.

Three gates here today:
  - required_threshold:  is the signal's score high enough?
  - in_cooldown:         did we trade this symbol too recently?
  - at_max_positions:    are we already at the position cap?

Other gates (whipsaw, Asia session, score-gap) live inside generate_signal
because they're tightly coupled to scoring; those stay in the signal layer.
"""

from __future__ import annotations

from typing import Optional

from ..core.types import SignalEngine


def required_threshold(
    signal_engine: SignalEngine,
    strategy: str,
    trend_threshold: float,
    breakout_threshold: float,
) -> Optional[float]:
    """Minimum score a signal must have to open a position.

    Returns None if no threshold applies (rule-match leg bypasses scoring —
    rulebook membership is the gate). Otherwise returns the breakout or trend
    threshold based on the signal's strategy.

    Rule leg detection uses the strategy prefix: any strategy starting with
    "rule_" or containing "statistical" is treated as a rule leg.
    """
    if signal_engine == "rule_match":
        return None
    if signal_engine == "combined" and ("rule_" in strategy or "statistical" in strategy):
        return None
    if "breakout" in strategy:
        return breakout_threshold
    return trend_threshold


def in_cooldown(elapsed: float, required: float) -> bool:
    """True if not enough time has passed since the last signal.

    Units don't matter — caller passes elapsed and required in the same
    unit (seconds for live, candle indices for backtest).
    """
    if elapsed < 0 or required <= 0:
        return False
    return elapsed < required


def at_max_positions(open_count: int, max_positions: int) -> bool:
    """True if we've already hit the concurrent-position cap."""
    return open_count >= max_positions
