"""TA scoring engine: regime detection, strategy scoring, TP/SL suggestion.

Strategies live here today (selected based on detected regime):
  - trend_pullback  (ADX > 25 or weak_trend)
  - [breakout was removed — dragged returns/Sharpe in backtests]
  - [mean reversion was removed — was losing in backtests]

score_technical() is the public entry; it dispatches based on detect_regime().
"""

from __future__ import annotations

from typing import Optional

from ..core.indicators import (
    adx,
    bollinger_bandwidth,
    ema,
    ema_alignment,
    macd,
    rsi,
    volume_ratio,
)
from ..core.types import Direction
from ..logging_setup import dbg


def detect_regime(adx_val: float, bb_squeeze: bool, vol_r: float,
                  closes: list) -> str:
    """
    Classify current market regime:
    - 'trending':  ADX > 25, clear directional movement
    - 'weak_trend': ADX < 25, transitional (scored with softened trend logic)

    NOTE: Mean reversion disabled (was causing losses in 2024-2025).
    All ADX < 20 regimes now use weak_trend (trend pullback logic).

    NOTE: Breakout regime removed — the custom score_breakout() strategy dragged
    returns and Sharpe in 12mo backtests. Squeeze+volume candles now fall through
    to ADX-based trend classification. bb_squeeze/vol_r are retained in the
    signature for call-site compatibility and debug logging.
    """
    # Strong trend
    if adx_val >= 25:
        return "trending"

    # Weak/forming trend (includes former "ranging" — now uses trend pullback)
    if adx_val >= 20:
        return "weak_trend"

    # ADX < 20 (was "ranging"): Now use weak trend instead of mean reversion
    return "weak_trend"

# ─────────────────────────────────────────────────────────────────
# STRATEGY 1: TREND PULLBACK (original V3, softened penalties)
# Best when: ADX > 25, clean EMA alignment
# ─────────────────────────────────────────────────────────────────
def score_trend_pullback(closes: list, volumes: list, rsi_val: float,
                         macd_line: float, signal_line: float,
                         hist_curr: float, hist_prev: float,
                         bull_align: bool, bear_align: bool,
                         adx_val: float = 25, above_e200: bool = True, below_e200: bool = False) -> tuple:
    """
    V3 Trend Pullback with regime-aware RSI zones and macro trend filtering (EMA200).
    Max theoretical: ~7.5 points per direction.
    """
    long_score = 0.0
    short_score = 0.0

    if adx_val > 50:
        rsi_min_long, rsi_max_long = 45, 65
        rsi_min_short, rsi_max_short = 55, 75
    else:
        rsi_min_long, rsi_max_long = 35, 55
        rsi_min_short, rsi_max_short = 45, 65

    # LONG: Bull alignment + MACD bullish (strong signal)
    # OR just MACD bullish (weaker but still valid)
    # Add bonus if RSI is in ideal zone, penalty if overbought
    if bull_align and macd_line > signal_line:
        long_score += 4.0  # Strong: EMA + MACD aligned
        if rsi_min_long <= rsi_val <= rsi_max_long:
            long_score += 1.5  # Bonus: RSI in sweet spot
        elif rsi_val < rsi_min_long:
            long_score += 1.0  # Bonus: RSI oversold (strong signal)
        elif rsi_val > rsi_max_long:
            long_score -= 0.5  # Penalty: RSI overbought (risky)
    elif macd_line > signal_line:
        # MACD momentum only (no EMA alignment) — weaker base
        long_score += 2.5
        if rsi_val < rsi_min_long:
            long_score += 0.75  # Bonus: RSI oversold helps
        elif rsi_val > rsi_max_long:
            long_score -= 0.25  # Penalty: overbought still risky

    # SHORT: Bear alignment + MACD bearish (strong signal)
    # OR just MACD bearish (weaker but still valid)
    # Add bonus if RSI is in ideal zone, penalty if oversold
    if bear_align and macd_line < signal_line:
        short_score += 4.0  # Strong: EMA + MACD aligned
        if rsi_min_short <= rsi_val <= rsi_max_short:
            short_score += 1.5  # Bonus: RSI in sweet spot
        elif rsi_val > rsi_max_short:
            short_score += 1.0  # Bonus: RSI overbought (strong signal)
        elif rsi_val < rsi_min_short:
            short_score -= 0.5  # Penalty: RSI oversold (risky)
    elif macd_line < signal_line:
        # MACD momentum only (no EMA alignment) — weaker base
        short_score += 2.5
        if rsi_val > rsi_max_short:
            short_score += 0.75  # Bonus: RSI overbought helps
        elif rsi_val < rsi_min_short:
            short_score -= 0.25  # Penalty: oversold still risky

    # MACD histogram confirmation
    if hist_curr > 0 and hist_curr > hist_prev:
        long_score += 1.5
    if hist_curr < 0 and hist_curr < hist_prev:
        short_score += 1.5

    # Volume confirmation — only boost the leading direction
    vol_r = volume_ratio(volumes)
    if vol_r > 1.3:
        vol_bonus = 1.0
    elif vol_r > 1.1:
        vol_bonus = 0.5
    else:
        vol_bonus = 0.0

    if vol_bonus > 0:
        if long_score >= short_score:
            long_score += vol_bonus
        else:
            short_score += vol_bonus

    # ── Macro trend filter (EMA200): Soft penalty for counter-trend trades ──
    if long_score > 0 and not above_e200:
        long_score -= 1.5  # Penalty: LONG against macro downtrend
    if short_score > 0 and not below_e200:
        short_score -= 1.5  # Penalty: SHORT against macro uptrend

    return long_score, short_score


# ─────────────────────────────────────────────────────────────────
# STRATEGY 3: BREAKOUT — REMOVED
# The custom breakout scorer (Bollinger squeeze + volume release) dragged
# returns and Sharpe in 12mo backtests. Removed along with the "breakout"
# regime; squeeze+volume candles now fall through to trend scoring.
# ─────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────
# ATR-BASED TP/SL — Single implementation (no more trade_setup duplicate)
# ─────────────────────────────────────────────────────────────────
def suggest_tp_sl(candles_1h: list, direction: Direction,
                  multiplier_sl: float = 1.5, rr_ratio: float = 2.0) -> dict:
    """
    Compute TP/SL levels from ATR and risk/reward ratio.

    OHLCV indexing: [0=open, 1=high, 2=low, 3=close, 4=volume]

    Returns dict with entry_price, suggested_tp, suggested_sl, sl_pct, tp_pct, atr.
    """
    closes = [float(c[3]) for c in candles_1h]
    highs  = [float(c[1]) for c in candles_1h]
    lows   = [float(c[2]) for c in candles_1h]

    entry_price = closes[-1]

    # ATR (20-period)
    tr_values = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_values.append(tr)

    atr = sum(tr_values[-20:]) / min(20, len(tr_values))

    sl_distance = atr * multiplier_sl
    tp_distance = sl_distance * rr_ratio

    if direction == "LONG":
        suggested_sl = entry_price - sl_distance
        suggested_tp = entry_price + tp_distance
    else:
        suggested_sl = entry_price + sl_distance
        suggested_tp = entry_price - tp_distance

    sl_pct = (sl_distance / entry_price) * 100
    tp_pct = (tp_distance / entry_price) * 100

    return {
        "entry_price": round(entry_price, 8),
        "suggested_sl": round(suggested_sl, 8),
        "suggested_tp": round(suggested_tp, 8),
        "sl_pct": round(sl_pct, 2),
        "tp_pct": round(tp_pct, 2),
        "atr": round(atr, 8),
        "sl_atr_mult": multiplier_sl,
        "rr_ratio": rr_ratio,
    }

def score_technical(symbol: str, candles_1h: list, precomputed_indicators: dict = None) -> dict:
    """
    Multi-regime technical scoring.
    Detects market regime (trending/weak_trend) and applies
    the appropriate strategy. Returns the best signal from the active regime.

    Args:
        symbol: Trading pair
        candles_1h: OHLCV candles
        precomputed_indicators: Optional dict with pre-computed indicator values (optimization for backtester).
                               If provided, uses these instead of computing.
    """
    closes  = [c[3] for c in candles_1h]
    volumes = [c[4] for c in candles_1h]
    highs   = [c[1] for c in candles_1h]
    lows    = [c[2] for c in candles_1h]

    if len(closes) < 50:
        return {"score": 0, "direction": "NEUTRAL", "long_score": 0, "short_score": 0, "details": {}}

    # ── Use pre-computed indicators if provided (optimization), else compute ──
    if precomputed_indicators:
        # Use pre-computed cheap indicators (RSI, MACD, EMA)
        rsi_val = precomputed_indicators.get('rsi', 50.0)
        macd_line_val = precomputed_indicators.get('macd_line', 0.0)
        sig_line_val = precomputed_indicators.get('macd_signal', 0.0)
        hist_curr = precomputed_indicators.get('macd_hist', 0.0)
        hist_prev = precomputed_indicators.get('macd_hist_prev', 0.0)
        e9 = precomputed_indicators.get('e9')
        e21 = precomputed_indicators.get('e21')
        e50 = precomputed_indicators.get('e50')
        bull_align = precomputed_indicators.get('bull_align', False)
        bear_align = precomputed_indicators.get('bear_align', False)
        e200 = precomputed_indicators.get('e200')
        above_e200 = precomputed_indicators.get('above_e200', True)
        below_e200 = precomputed_indicators.get('below_e200', False)

        # Use pre-computed expensive indicators if available, else compute on-demand
        adx_val = precomputed_indicators.get('adx') if 'adx' in precomputed_indicators else adx(highs, lows, closes, period=14)
        vol_r = precomputed_indicators.get('vol_ratio') if 'vol_ratio' in precomputed_indicators else volume_ratio(volumes)
        bb_bw = precomputed_indicators.get('bb_bandwidth') if 'bb_bandwidth' in precomputed_indicators else bollinger_bandwidth(closes)[0]
        bb_squeeze = precomputed_indicators.get('bb_squeeze') if 'bb_squeeze' in precomputed_indicators else bollinger_bandwidth(closes)[1]
    else:
        # Compute all indicators once (slower path, for live scanner)
        rsi_val = rsi(closes)
        macd_line_val, sig_line_val, hist_curr = macd(closes)
        hist_prev = macd(closes[:-1])[2] if len(closes) > 35 else hist_curr
        vol_r = volume_ratio(volumes)
        e9, e21, e50, bull_align, bear_align = ema_alignment(closes)
        adx_val = adx(highs, lows, closes, period=14)

        # 200-EMA macro trend filter (migrated to 1H: use 800 to maintain ~33-day lookback)
        # 4H system: 200 × 4h = 800 hours ≈ 33 days
        # 1H system: 800 × 1h = 800 hours ≈ 33 days (equivalent scale)
        e200 = ema(closes, min(800, len(closes)))[-1]
        above_e200 = closes[-1] > e200
        below_e200 = closes[-1] < e200

        # Bollinger bandwidth & squeeze detection
        bb_bw, bb_squeeze = bollinger_bandwidth(closes)

    # ── Detect market regime ──
    regime = detect_regime(adx_val, bb_squeeze, vol_r, closes)

    # Debug: Log regime and key indicators
    dbg.debug(f"[{symbol}] Regime={regime} | ADX={adx_val:.1f} BB_squeeze={bb_squeeze} vol_r={vol_r:.2f}")
    dbg.debug(f"[{symbol}] Indicators | RSI={rsi_val:.1f} MACD={macd_line_val:.6f} sig={sig_line_val:.6f} hist={hist_curr:.6f} prev={hist_prev:.6f}")
    dbg.debug(f"[{symbol}] EMA align | bull={bull_align} bear={bear_align} above_e200={above_e200} e200={e200:.2f}")

    # ── Score using regime-appropriate strategy ──
    if regime == "trending":
        long_pts, short_pts = score_trend_pullback(
            closes, volumes, rsi_val, macd_line_val, sig_line_val,
            hist_curr, hist_prev, bull_align, bear_align, adx_val,
            above_e200=above_e200, below_e200=below_e200
        )
        strategy = "trend_pullback"

    elif regime == "weak_trend":
        # Use trend pullback with softened ADX penalty (mean reversion disabled)
        long_pts, short_pts = score_trend_pullback(
            closes, volumes, rsi_val, macd_line_val, sig_line_val,
            hist_curr, hist_prev, bull_align, bear_align, adx_val,
            above_e200=above_e200, below_e200=below_e200
        )

        # Gradient ADX penalty for weak trend (0.5-0.8x in ADX 20-25 range)
        # Floor at 0.3 to prevent negative multipliers when ADX < ~16.7
        adx_mult = max(0.3, 0.5 + (adx_val - 20) / 5 * 0.3)
        long_pts *= adx_mult
        short_pts *= adx_mult
        dbg.debug(f"[{symbol}] ADX mult={adx_mult:.2f} → LONG={long_pts:.2f} SHORT={short_pts:.2f}")

        strategy = "trend_pullback_weak"

    # NOTE: Breakout regime removed — score_breakout() deleted; detect_regime()
    # no longer returns "breakout", so squeeze+volume candles are scored as trend.
    # NOTE: Mean reversion regime removed (was losing -$475 in 12mo backtest)
    # All low-ADX conditions now use weak_trend (trend pullback logic)

    long_pts  = round(long_pts,  2)
    short_pts = round(short_pts, 2)
    dbg.debug(f"[{symbol}] Raw scores | LONG={long_pts:.2f} SHORT={short_pts:.2f}")

    details = {
        "rsi":         {"1h": round(rsi_val, 1)},
        "macd_line":   round(macd_line_val, 6),
        "signal_line": round(sig_line_val, 6),
        "hist_curr":   round(hist_curr, 6),
        "hist_prev":   round(hist_prev, 6),
        "vol_ratio":   round(vol_r, 2),
        "adx":         round(adx_val, 1),
        "ema200":      round(e200, 4),
        "above_e200":  above_e200,
        "ema_bull":    bull_align,
        "ema_bear":    bear_align,
        "regime":      regime,
        "strategy":    strategy,
        "bb_bandwidth": bb_bw,
        "bb_squeeze":  bb_squeeze,
        "scoring":     f"multi_regime_{strategy}",
    }

    if long_pts >= short_pts:
        return {"score": long_pts, "direction": "LONG",  "long_score": long_pts, "short_score": short_pts, "details": details}
    else:
        return {"score": short_pts, "direction": "SHORT", "long_score": long_pts, "short_score": short_pts, "details": details}
