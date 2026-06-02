"""Pure indicator math and the Snapshot dataclass.

All functions are stateless transforms over price/volume lists.
No I/O, no logging, no exchange calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


# ── Pure math ────────────────────────────────────────────────────────────────

def rsi(closes: list, period: int = 14) -> float:
    """RSI with Wilder smoothing (matches TradingView/Binance)."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = np.mean(gains[:period])
    avg_l = np.mean(losses[:period])
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100 - (100 / (1 + avg_g / avg_l))


def ema(values: list, period: int) -> list:
    """EMA with SMA seed (matches TradingView). Returns shorter list than input."""
    if not values:
        return []
    result = []
    k = 2 / (period + 1)
    seed_period = min(period, len(values))
    seed_value = np.mean(values[:seed_period])
    for i, v in enumerate(values):
        if i < seed_period:
            if i == seed_period - 1:
                result.append(seed_value)
        else:
            result.append(v * k + result[-1] * (1 - k))
    return result


def macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram)."""
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [ema_fast[offset + i] - ema_slow[i] for i in range(len(ema_slow))]
    sig_line = ema(macd_line, signal)
    offset2 = len(macd_line) - len(sig_line)
    hist = [macd_line[offset2 + i] - sig_line[i] for i in range(len(sig_line))]
    return macd_line[-1], sig_line[-1], hist[-1]


def bollinger(closes: list, period: int = 20, std_mult: float = 2.0):
    """Returns (mid, upper, lower)."""
    if len(closes) < period:
        return closes[-1], closes[-1] * 1.02, closes[-1] * 0.98
    window = closes[-period:]
    mid = np.mean(window)
    std = np.std(window)
    return mid, mid + std_mult * std, mid - std_mult * std


def bollinger_bandwidth(closes: list, period: int = 20, std_mult: float = 2.0) -> tuple:
    """Returns (bandwidth_pct, is_squeeze). Squeeze = below 20th percentile of last 100."""
    if len(closes) < max(period, 100):
        return 5.0, False
    bw_history = []
    for i in range(100, 0, -1):
        idx = len(closes) - i
        if idx < period:
            continue
        window = closes[idx - period:idx]
        mid = np.mean(window)
        std = np.std(window)
        if mid > 0:
            bw_history.append((std * std_mult * 2) / mid * 100)
    mid_now, upper_now, lower_now = bollinger(closes, period, std_mult)
    bw_now = (upper_now - lower_now) / mid_now * 100 if mid_now > 0 else 5.0
    is_squeeze = bool(bw_history) and bw_now < np.percentile(bw_history, 20)
    return round(bw_now, 2), is_squeeze


def volume_ratio(volumes: list, period: int = 20) -> float:
    if len(volumes) < period + 1:
        return 1.0
    avg = np.mean(volumes[-period - 1:-1])
    return volumes[-1] / avg if avg > 0 else 1.0


def adx(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """Average Directional Index (0-100). >25 = trending, <20 = choppy."""
    if len(highs) < period * 2:
        return 0.0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(highs)):
        h_diff = highs[i] - highs[i - 1]
        l_diff = lows[i - 1] - lows[i]
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)

    def wilder(vals, p):
        result = [sum(vals[:p])]
        for v in vals[p:]:
            result.append(result[-1] - result[-1] / p + v)
        return result

    atr_s = wilder(trs, period)
    pdm_s = wilder(plus_dm, period)
    mdm_s = wilder(minus_dm, period)
    dx_vals = []
    for a, p, m in zip(atr_s, pdm_s, mdm_s):
        if a == 0:
            dx_vals.append(0.0)
            continue
        pdi = 100 * p / a
        mdi = 100 * m / a
        dx_vals.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0)
    if len(dx_vals) < period:
        return 0.0
    adx_val = float(np.mean(dx_vals[:period]))
    for dx_v in dx_vals[period:]:
        adx_val = (adx_val * (period - 1) + dx_v) / period
    return adx_val


def market_structure(candles_1h: list) -> tuple[bool, bool]:
    """Returns (higher_highs, lower_lows)."""
    if len(candles_1h) < 6:
        return False, False
    highs = [c[1] for c in candles_1h[-6:]]
    lows = [c[2] for c in candles_1h[-6:]]
    hh = all(highs[i] > highs[i - 1] for i in range(1, len(highs)))
    ll = all(lows[i] < lows[i - 1] for i in range(1, len(lows)))
    return hh, ll


def atr(candles: list, period: int = 20) -> float:
    """Average True Range over `period` candles. Candles as [open, high, low, close, ...]."""
    if len(candles) < 2:
        return 0.0
    closes = [c[3] for c in candles]
    highs = [c[1] for c in candles]
    lows = [c[2] for c in candles]
    tr_values = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    if not tr_values:
        return 0.0
    return sum(tr_values[-period:]) / min(period, len(tr_values))


def ema_alignment(closes: list):
    """Returns (ema9, ema21, ema50, bullish_aligned, bearish_aligned)."""
    if len(closes) < 50:
        return None, None, None, False, False
    e9 = ema(closes, 9)[-1]
    e21 = ema(closes, 21)[-1]
    e50 = ema(closes, 50)[-1]
    return e9, e21, e50, e9 > e21 > e50, e9 < e21 < e50


def support_resistance(candles_1h: list, price: float):
    """Returns (near_support, near_resistance, dist_support_pct, dist_resist_pct)."""
    if len(candles_1h) < 10:
        return False, False, 999, 999
    recent = candles_1h[-10:]
    resistance = max(c[1] for c in recent)
    support = min(c[2] for c in recent)
    dist_r = (resistance - price) / price * 100
    dist_s = (price - support) / price * 100
    return 0 < dist_s < 1.5, 0 < dist_r < 1.5, dist_s, dist_r


# ── Snapshot dataclass ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Snapshot:
    """All indicator values for a single candle window, ready for rule matching."""
    # Price
    close: float
    open: float
    high: float
    low: float
    prev_open: float
    prev_high: float
    prev_low: float
    prev_close: float
    # RSI
    rsi: float
    # EMAs
    e9: float
    e21: float
    e50: float
    e200: float
    e9_prev: float
    e21_prev: float
    e50_prev: float
    e200_prev: float
    above_ema200: bool
    below_ema200: bool
    # MACD
    macd_line: float
    macd_signal: float
    macd_hist: float
    macd_hist_prev: float
    # Trend strength
    adx: float
    vol_ratio: float
    # Bollinger
    bb_mid: float
    bb_upper: float
    bb_lower: float
    bb_bandwidth: float
    bb_squeeze: bool
    price_near_upper_bb: bool
    price_near_lower_bb: bool
    # Market structure
    higher_highs: bool
    lower_lows: bool
    # Candle patterns
    bullish_engulfing: bool
    bearish_engulfing: bool
    inside_bar: bool
    outside_bar: bool
    pin_bar_bull: bool
    pin_bar_bear: bool
    three_green_candles: bool
    three_red_candles: bool
    # ATR
    atr20: float
    atr20_prev: float
    atr_percentile_high: bool
    atr_expansion: bool
    candle_range_above_atr: bool
    two_expansion_green_candles: bool
    two_expansion_red_candles: bool
    # EMA momentum
    ema21_rising: bool
    ema21_falling: bool
    ema_fan_wide: bool
    strong_ema21_slope_up: bool
    strong_ema21_slope_down: bool
    adx_rising: bool
    adx_falling: bool
    # Price levels
    near_20bar_high: bool
    near_20bar_low: bool
    breaks_20bar_high: bool
    breaks_20bar_low: bool
    # Divergence
    rsi_bullish_divergence: bool
    rsi_bearish_divergence: bool
    macd_bullish_divergence: bool
    macd_bearish_divergence: bool
    # HTF
    htf_4h_bull_trend: bool
    htf_4h_bear_trend: bool
    # Meta
    hour_utc: int
    symbol: str

    def to_dict(self) -> dict:
        """For backward compat with code that still accesses indicators as a dict."""
        import dataclasses
        return dataclasses.asdict(self)
