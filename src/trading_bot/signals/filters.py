"""Signal-side filters: whipsaw detection, Asia-session gate, rejection cache."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from ..logging_setup import dbg


# Module-level rejection-reason cache (per-symbol). The trader reads this
# after each generate_signal() call to surface why a symbol didn't fire.
_last_rejection_reason: dict[str, str] = {}


def is_whipsaw(state: dict, symbol: str, new_direction: str) -> bool:
    """Check if signal direction changed in last 5 minutes (prevent false reversals)."""
    if "signal_history" not in state:
        state["signal_history"] = {}

    key = symbol
    last_signal = state["signal_history"].get(key)
    now = time.time()

    if last_signal and now - last_signal["ts"] < 300:  # Within 5 minutes
        if last_signal["direction"] != new_direction and new_direction != "NEUTRAL":
            dbg.debug(f"[{symbol}] WHIPSAW DETECTED: was {last_signal['direction']}, now {new_direction} (in {now - last_signal['ts']:.0f}s)")
            return True

    state["signal_history"][key] = {"direction": new_direction, "ts": now}
    return False


def mark_signal(state: dict, symbol: str, direction: str):
    """Record signal for whipsaw detection."""
    if "signal_history" not in state:
        state["signal_history"] = {}
    state["signal_history"][symbol] = {"direction": direction, "ts": time.time()}


def _is_asia_session(current_time: Optional[datetime] = None) -> bool:
    """Check if current time is in Asia session (UTC 0-7, low liquidity)."""
    time_for_filter = current_time if current_time is not None else datetime.now(timezone.utc)
    hour_utc = time_for_filter.hour
    return 0 <= hour_utc < 8
