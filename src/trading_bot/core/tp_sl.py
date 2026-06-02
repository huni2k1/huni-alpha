"""Take-profit and stop-loss price math.

Given a direction, entry price, ATR, and the template multipliers, return
the absolute TP and SL prices. Pure function — no I/O, no exchange calls.

Always compute these at the ACTUAL fill price (not the signal-time price)
so the distances — and therefore the risk/reward — match what the strategy
was validated on. Replaces the older "compute at signal price, then re-anchor"
pattern with a single, direct computation.
"""

from __future__ import annotations

try:
    from .types import Direction
except ImportError:
    from types import Direction  # type: ignore


def compute_tp_sl(
    direction: Direction,
    entry_price: float,
    atr: float,
    sl_atr_mult: float,
    rr_ratio: float,
) -> tuple[float, float]:
    """Return (tp_price, sl_price) given the actual entry and the template.

    sl_distance = atr × sl_atr_mult
    tp_distance = sl_distance × rr_ratio
    LONG:  TP above entry, SL below entry
    SHORT: TP below entry, SL above entry
    """
    sl_distance = atr * sl_atr_mult
    tp_distance = sl_distance * rr_ratio

    if direction == "LONG":
        return entry_price + tp_distance, entry_price - sl_distance
    return entry_price - tp_distance, entry_price + sl_distance
