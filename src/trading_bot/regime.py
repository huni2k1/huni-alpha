"""BTC market regime classifier.

Regime is determined by the state of BTC's 200-period EMA on the 1h timeframe:
  - bull: close > ema200 AND ema200 rising over the last `slope_lookback` candles
  - bear: close < ema200 AND ema200 falling over the last `slope_lookback` candles
  - chop: anything else (crossing, flat, transitioning)

Used by:
  - setup_analyzer to segment research rows into regime-specific views
  - scanner at runtime to filter validated setups by their `scope_regime`

Design notes:
  - Runs on BTC only (it leads the alt complex). Every symbol uses BTC's regime
    at the same timestamp, not its own.
  - Warmup: first `ema_period` candles produce no label (returns "unknown") —
    ignored by row tagging.
"""

from __future__ import annotations

from typing import Optional

from .core.indicators import ema
from .core.types import MarketRegime


# Local alias retained for backward compat with any external callers.
Regime = MarketRegime
VALID_REGIMES: tuple[str, ...] = ("bull", "bear", "chop")

DEFAULT_EMA_PERIOD = 200
DEFAULT_SLOPE_LOOKBACK = 20  # ~20h on 1h candles; tolerates minor wiggle


def classify_regime_series(
    candles: list[dict],
    ema_period: int = DEFAULT_EMA_PERIOD,
    slope_lookback: int = DEFAULT_SLOPE_LOOKBACK,
) -> list[MarketRegime]:
    """Return regime label per candle, aligned to input length.

    First `ema_period + slope_lookback - 1` entries are "unknown" (warmup).
    """
    n = len(candles)
    if n == 0:
        return []

    closes = [float(c["close"]) for c in candles]
    ema_vals = ema(closes, ema_period)
    # `ema` drops the first (ema_period - 1) entries; align with right-edge padding of None.
    offset = n - len(ema_vals)
    aligned_ema: list[Optional[float]] = [None] * offset + list(ema_vals)

    labels: list[MarketRegime] = []
    for i in range(n):
        ema_now = aligned_ema[i]
        ema_then = aligned_ema[i - slope_lookback] if i - slope_lookback >= 0 else None
        if ema_now is None or ema_then is None:
            labels.append("unknown")
            continue

        close = closes[i]
        slope_up = ema_now > ema_then
        slope_down = ema_now < ema_then

        if close > ema_now and slope_up:
            labels.append("bull")
        elif close < ema_now and slope_down:
            labels.append("bear")
        else:
            labels.append("chop")
    return labels


def classify_current_regime(
    candles: list[dict],
    ema_period: int = DEFAULT_EMA_PERIOD,
    slope_lookback: int = DEFAULT_SLOPE_LOOKBACK,
) -> MarketRegime:
    """Regime for the latest candle. Returns 'unknown' if history is too short."""
    series = classify_regime_series(candles, ema_period, slope_lookback)
    return series[-1] if series else "unknown"


def build_regime_lookup(
    btc_candles: list[dict],
    ema_period: int = DEFAULT_EMA_PERIOD,
    slope_lookback: int = DEFAULT_SLOPE_LOOKBACK,
) -> dict[int, MarketRegime]:
    """Map close_time (ms) -> regime label for each BTC candle.

    Analyzer uses this to tag every row (across symbols) with the BTC regime
    that held at the row's timestamp.
    """
    labels = classify_regime_series(btc_candles, ema_period, slope_lookback)
    return {int(c["close_time"]): label for c, label in zip(btc_candles, labels)}
