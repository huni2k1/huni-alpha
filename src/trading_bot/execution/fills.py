"""Fill-resolution helpers shared by the backtester and the analyzer's simulator."""

from __future__ import annotations

import random
from typing import Literal

from ..core.types import TieBreak


def resolve_same_candle_hit(
    candle: dict,
    tp_price: float,
    sl_price: float,
    *,
    tie_break: TieBreak = "random",
) -> Literal["TP", "SL"]:
    """Pick TP or SL when a single candle's range touches both.

    1h candles hide intra-candle tick order. We use the candle open as the
    observable starting point and infer that whichever level (TP or SL) is
    closer to the open is more likely to be hit first — less distance to
    travel.

    This covers the natural gap cases for free: if the candle opens past TP,
    its distance to TP is small and to SL is large → TP. Likewise for SL.

    tie_break controls what happens when open is exactly equidistant from
    both (rare, but possible):
      - "random": coin flip — used by live/backtest where we can't know.
      - "sl":     deterministic SL — used by the analyzer so research runs
                  reproduce bit-for-bit across reruns.
    """
    candle_open = float(candle["open"])
    distance_to_tp = abs(float(tp_price) - candle_open)
    distance_to_sl = abs(float(sl_price) - candle_open)

    if distance_to_tp < distance_to_sl:
        return "TP"
    if distance_to_sl < distance_to_tp:
        return "SL"

    if tie_break == "sl":
        return "SL"
    return random.choice(["TP", "SL"])
