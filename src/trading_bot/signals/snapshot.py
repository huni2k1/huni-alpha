"""Indicator snapshot — turn raw candles into a typed Snapshot ready for rule matching.

Two entry points:
  - precompute_indicators_for_all_candles: bulk pre-compute for the backtester
  - _build_indicator_snapshot: single-candle snapshot used at signal time
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import numpy as np

from ..core.indicators import (
    Snapshot,
    adx,
    atr as _atr_fn,
    bollinger,
    bollinger_bandwidth,
    ema,
    market_structure,
    macd,
    rsi,
    volume_ratio,
)


def precompute_indicators_for_all_candles(candles: list) -> dict:
    """
    Pre-compute cheap indicators (RSI, MACD, EMA) for every candle.

    This optimization caches indicator values that are expensive to compute
    repeatedly. Expected 1.5-2x speedup (ADX/Bollinger still computed on-demand).

    Args:
        candles: OHLCV format [[open, high, low, close, volume], ...]

    Returns:
        Dict mapping candle index to pre-computed indicators dict.

    Note:
        ADX and Bollinger Bandwidth are computed on-demand (too expensive to pre-compute).
        Volume ratio is fast to compute, so also done on-demand.
    """
    if len(candles) < 50:
        return {}

    closes = [c[3] for c in candles]
    highs_all = [c[1] for c in candles]
    lows_all = [c[2] for c in candles]
    volumes = [c[4] for c in candles]

    indicators_cache = {}

    # Pre-compute full series for EMA (these are fast and heavily used)
    ema_9_series = ema(closes, 9)
    ema_21_series = ema(closes, 21)
    ema_50_series = ema(closes, 50)
    ema_200_series = ema(closes, min(800, len(closes)))

    # Pre-compute ATR series (O(n) rolling average of true-range).
    # Avoids the per-candle loop of ~100 full recomputes in _build_indicator_snapshot.
    atr_period = 20
    tr_series: list[float] = []
    for i in range(1, len(candles)):
        h, l, cp = highs_all[i], lows_all[i], closes[i - 1]
        tr_series.append(max(h - l, abs(h - cp), abs(l - cp)))
    # atr_series[i] = ATR at candle index i+1 (first candle has no TR)
    atr_series: list[float] = [0.0]
    _tr_window: list[float] = []
    _tr_sum = 0.0
    for tr in tr_series:
        _tr_window.append(tr)
        _tr_sum += tr
        if len(_tr_window) > atr_period:
            _tr_sum -= _tr_window.pop(0)
        atr_series.append(_tr_sum / len(_tr_window))

    # Pre-compute RSI series (Wilder smoothing)
    rsi_series = []
    if len(closes) >= 15:
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i - 1]
            gains.append(max(d, 0))
            losses.append(max(-d, 0))

        avg_g = np.mean(gains[:14])
        avg_l = np.mean(losses[:14])

        # Prepend dummy values for the initial period
        rsi_series = [50.0] * 14

        for i in range(14, len(gains)):
            avg_g = (avg_g * 13 + gains[i]) / 14
            avg_l = (avg_l * 13 + losses[i]) / 14
            if avg_l == 0:
                rsi_series.append(100.0)
            else:
                rs = avg_g / avg_l
                rsi_series.append(100 - (100 / (1 + rs)))

    # Pre-compute MACD series (cheap relative to ADX)
    ema_12 = ema(closes, 12)
    ema_26 = ema(closes, 26)
    offset = len(ema_12) - len(ema_26)
    macd_line_series = [ema_12[offset + i] - ema_26[i] for i in range(len(ema_26))]
    macd_signal_series = ema(macd_line_series, 9)

    # Align histogram
    offset2 = len(macd_line_series) - len(macd_signal_series)
    macd_hist_series = [macd_line_series[offset2 + i] - macd_signal_series[i]
                       for i in range(len(macd_signal_series))]

    # Build cache using pre-computed series (no expensive per-position calculations)
    for t in range(50, len(candles)):
        # Index into pre-computed series
        # rsi_series[k] uses gains[k] = closes[k+1]-closes[k], so rsi_series[k]
        # incorporates data through candle k+1. Use t-1 to avoid 1-candle look-ahead.
        idx_rsi = min(t - 1, len(rsi_series) - 1)
        rsi_val = rsi_series[idx_rsi] if idx_rsi >= 0 else 50.0

        # MACD: align to current position
        idx_macd = t - (len(closes) - len(macd_line_series))
        if idx_macd >= 0 and idx_macd < len(macd_line_series):
            macd_line_val = macd_line_series[idx_macd]
            idx_signal = idx_macd - (len(macd_line_series) - len(macd_signal_series))
            macd_signal_val = macd_signal_series[idx_signal] if idx_signal >= 0 else 0
            idx_hist = idx_macd - (len(macd_line_series) - len(macd_hist_series))
            macd_hist_val = macd_hist_series[idx_hist] if idx_hist >= 0 else 0
            macd_hist_prev = macd_hist_series[idx_hist - 1] if idx_hist > 0 and idx_hist < len(macd_hist_series) else macd_hist_val
        else:
            macd_line_val = macd_signal_val = macd_hist_val = macd_hist_prev = 0

        # EMA alignment (index into pre-computed series)
        idx_e9 = t - (len(closes) - len(ema_9_series))
        idx_e21 = t - (len(closes) - len(ema_21_series))
        idx_e50 = t - (len(closes) - len(ema_50_series))
        idx_e200 = t - (len(closes) - len(ema_200_series))

        e9 = ema_9_series[idx_e9] if 0 <= idx_e9 < len(ema_9_series) else None
        e21 = ema_21_series[idx_e21] if 0 <= idx_e21 < len(ema_21_series) else None
        e50 = ema_50_series[idx_e50] if 0 <= idx_e50 < len(ema_50_series) else None
        e200 = ema_200_series[idx_e200] if 0 <= idx_e200 < len(ema_200_series) else None

        # Prev-candle EMA values — stored here so _build_indicator_snapshot can
        # use O(1) lookups instead of recomputing the full EMA series each call.
        e9_prev = ema_9_series[idx_e9 - 1] if 0 < idx_e9 < len(ema_9_series) else None
        e21_prev = ema_21_series[idx_e21 - 1] if 0 < idx_e21 < len(ema_21_series) else None
        e50_prev = ema_50_series[idx_e50 - 1] if 0 < idx_e50 < len(ema_50_series) else None
        e200_prev = ema_200_series[idx_e200 - 1] if 0 < idx_e200 < len(ema_200_series) else None

        bull_align = e9 and e21 and e50 and (e9 > e21 > e50)
        bear_align = e9 and e21 and e50 and (e9 < e21 < e50)
        above_e200 = e200 and (closes[t] > e200)
        below_e200 = e200 and (closes[t] < e200)

        # ATR at t and t-1 — read from precomputed series (O(1))
        atr20_val = atr_series[t] if t < len(atr_series) else 0.0
        atr20_prev_val = atr_series[t - 1] if t > 0 and (t - 1) < len(atr_series) else 0.0

        # ATR percentile: is atr20_val in the top 20% of the last 100 ATR values?
        lookback_start = max(0, t - 100)
        recent_atrs = [atr_series[i] for i in range(lookback_start, t) if atr_series[i] > 0]
        atr_pct_high = bool(recent_atrs) and atr20_val >= float(np.percentile(recent_atrs, 80))

        # RSI and MACD line windows (last 21 values) for divergence detection.
        # Stored as lists so _build_indicator_snapshot can avoid recomputing
        # full RSI/MACD series for each of the 20 lookback positions.
        rsi_win_size = 21
        rsi_window = [
            rsi_series[min(max(0, t - rsi_win_size + 1 + j), len(rsi_series) - 1)]
            for j in range(rsi_win_size)
        ]
        macd_win_size = 21
        macd_window = []
        for j in range(macd_win_size):
            raw_idx = t - macd_win_size + 1 + j
            ml_idx = raw_idx - (len(closes) - len(macd_line_series))
            if 0 <= ml_idx < len(macd_line_series):
                macd_window.append(macd_line_series[ml_idx])
            else:
                macd_window.append(0.0)

        # Cache the pre-computed values
        indicators_cache[t] = {
            'rsi': rsi_val,
            'macd_line': macd_line_val,
            'macd_signal': macd_signal_val,
            'macd_hist': macd_hist_val,
            'macd_hist_prev': macd_hist_prev,
            'e9': e9,
            'e21': e21,
            'e50': e50,
            'e200': e200,
            'e9_prev': e9_prev,
            'e21_prev': e21_prev,
            'e50_prev': e50_prev,
            'e200_prev': e200_prev,
            'bull_align': bull_align,
            'bear_align': bear_align,
            'above_e200': above_e200,
            'below_e200': below_e200,
            'atr20': atr20_val,
            'atr20_prev': atr20_prev_val,
            'atr_percentile_high': atr_pct_high,
            'rsi_window': rsi_window,
            'macd_line_window': macd_window,
            # Note: ADX, Bollinger, Volume Ratio computed on-demand in score_technical
        }

    return indicators_cache


def _build_indicator_snapshot(
    symbol: str,
    candles_1h: list,
    current_time: Optional[datetime] = None,
    precomputed_indicators: dict = None,
) -> Optional[Snapshot]:
    """Build the latest-candle feature snapshot used by validated setup matching."""
    closes = [c[3] for c in candles_1h]
    opens = [c[0] for c in candles_1h]
    volumes = [c[4] for c in candles_1h]
    highs = [c[1] for c in candles_1h]
    lows = [c[2] for c in candles_1h]
    if len(closes) < 50:
        return None

    if precomputed_indicators:
        rsi_val = precomputed_indicators.get("rsi", 50.0)
        macd_line_val = precomputed_indicators.get("macd_line", 0.0)
        macd_signal_val = precomputed_indicators.get("macd_signal", 0.0)
        macd_hist_val = precomputed_indicators.get("macd_hist", 0.0)
        macd_hist_prev = precomputed_indicators.get("macd_hist_prev", 0.0)
        e9 = precomputed_indicators.get("e9")
        e21 = precomputed_indicators.get("e21")
        e50 = precomputed_indicators.get("e50")
        e200 = precomputed_indicators.get("e200")
        above_e200 = bool(precomputed_indicators.get("above_e200", False))
        below_e200 = bool(precomputed_indicators.get("below_e200", False))
    else:
        rsi_val = rsi(closes)
        macd_line_val, macd_signal_val, macd_hist_val = macd(closes)
        macd_hist_prev = macd(closes[:-1])[2] if len(closes) > 27 else macd_hist_val
        e9 = ema(closes, 9)[-1] if len(closes) >= 9 else closes[-1]
        e21 = ema(closes, 21)[-1] if len(closes) >= 21 else closes[-1]
        e50 = ema(closes, 50)[-1] if len(closes) >= 50 else closes[-1]
        e200 = ema(closes, min(800, len(closes)))[-1]
        above_e200 = closes[-1] > e200
        below_e200 = closes[-1] < e200

    if e9 is None:
        e9 = ema(closes, 9)[-1] if len(closes) >= 9 else closes[-1]
    if e21 is None:
        e21 = ema(closes, 21)[-1] if len(closes) >= 21 else closes[-1]
    if e50 is None:
        e50 = ema(closes, 50)[-1] if len(closes) >= 50 else closes[-1]
    if e200 is None:
        e200 = ema(closes, min(800, len(closes)))[-1]
        above_e200 = closes[-1] > e200
        below_e200 = closes[-1] < e200

    # Use precomputed prev-candle EMA values when available (O(1)) to avoid
    # recomputing the full EMA series on every call (O(n) — Bug 7 in prior audit).
    _fallback_prev = closes[-2] if len(closes) >= 2 else closes[-1]
    if precomputed_indicators and precomputed_indicators.get('e9_prev') is not None:
        e9_prev = precomputed_indicators['e9_prev']
        e21_prev = precomputed_indicators.get('e21_prev') or _fallback_prev
        e50_prev = precomputed_indicators.get('e50_prev') or _fallback_prev
        e200_prev = precomputed_indicators.get('e200_prev') or _fallback_prev
    else:
        e9_prev = ema(closes[:-1], 9)[-1] if len(closes) >= 10 else _fallback_prev
        e21_prev = ema(closes[:-1], 21)[-1] if len(closes) >= 22 else _fallback_prev
        e50_prev = ema(closes[:-1], 50)[-1] if len(closes) >= 51 else _fallback_prev
        e200_prev = ema(closes[:-1], min(800, len(closes) - 1))[-1] if len(closes) >= 3 else _fallback_prev

    if precomputed_indicators and 'adx' in precomputed_indicators:
        adx_val = precomputed_indicators['adx']
        vol_r = precomputed_indicators.get('vol_ratio', volume_ratio(volumes))
        bb_mid = precomputed_indicators.get('bb_mid', bollinger(closes, 20, 2.0)[0])
        bb_upper = precomputed_indicators.get('bb_upper', bollinger(closes, 20, 2.0)[1])
        bb_lower = precomputed_indicators.get('bb_lower', bollinger(closes, 20, 2.0)[2])
        bb_bw = precomputed_indicators.get('bb_bandwidth', bollinger_bandwidth(closes)[0])
        bb_squeeze = precomputed_indicators.get('bb_squeeze', bollinger_bandwidth(closes)[1])
    else:
        adx_val = adx(highs, lows, closes, period=14)
        vol_r = volume_ratio(volumes)
        bb_mid, bb_upper, bb_lower = bollinger(closes, 20, 2.0)
        bb_bw, bb_squeeze = bollinger_bandwidth(closes)
    higher_highs, lower_lows = market_structure(candles_1h)
    time_for_filter = current_time if current_time is not None else datetime.now(timezone.utc)
    current_open = float(opens[-1])
    current_high = float(highs[-1])
    current_low = float(lows[-1])
    current_close = float(closes[-1])
    prev_open = float(opens[-2])
    prev_high = float(highs[-2])
    prev_low = float(lows[-2])
    prev_close = float(closes[-2])
    body = abs(current_close - current_open)
    upper_wick = current_high - max(current_open, current_close)
    lower_wick = min(current_open, current_close) - current_low
    bullish_engulfing = (
        current_close > current_open
        and prev_close < prev_open
        and current_open <= prev_close
        and current_close >= prev_open
    )
    bearish_engulfing = (
        current_close < current_open
        and prev_close > prev_open
        and current_open >= prev_close
        and current_close <= prev_open
    )
    inside_bar = current_high <= prev_high and current_low >= prev_low
    outside_bar = current_high >= prev_high and current_low <= prev_low
    pin_bar_bull = lower_wick >= body * 2.0 and upper_wick <= body and current_close >= current_open
    pin_bar_bear = upper_wick >= body * 2.0 and lower_wick <= body and current_close <= current_open
    three_green_candles = len(closes) >= 3 and all(closes[-i] > opens[-i] for i in (1, 2, 3))
    three_red_candles = len(closes) >= 3 and all(closes[-i] < opens[-i] for i in (1, 2, 3))
    if precomputed_indicators and precomputed_indicators.get('atr20') is not None:
        atr20 = float(precomputed_indicators['atr20'])
        atr20_prev = float(precomputed_indicators.get('atr20_prev') or atr20)
        atr_percentile_high = bool(precomputed_indicators.get('atr_percentile_high', False))
    else:
        atr20 = _atr_fn(candles_1h, 20)
        atr20_prev = _atr_fn(candles_1h[:-1], 20) if len(candles_1h) >= 21 else atr20
        recent_atr_values = [
            _atr_fn(candles_1h[:idx], 20)
            for idx in range(max(21, len(candles_1h) - 100), len(candles_1h))
        ]
        recent_atr_values = [v for v in recent_atr_values if v > 0]
        atr_percentile_high = bool(recent_atr_values) and atr20 >= float(np.percentile(recent_atr_values, 80))
    atr_expansion = atr20_prev > 0 and atr20 >= atr20_prev * 1.2
    candle_range_above_atr = atr20 > 0 and (current_high - current_low) / atr20 >= 1.2
    current_range_atr = (current_high - current_low) / atr20 if atr20 > 0 else 0.0
    prev_range = max(prev_high - prev_low, 0.0)
    prev_range_atr = prev_range / atr20_prev if atr20_prev > 0 else 0.0
    ema21_rising = float(e21) > float(e21_prev)
    ema21_falling = float(e21) < float(e21_prev)
    ema_fan_wide = abs(float(e9) - float(e50)) / max(abs(current_close), 1e-9) * 100.0 >= 1.0
    ema21_slope_pct = ((float(e21) - float(e21_prev)) / max(abs(current_close), 1e-9)) * 100.0
    adx_prev = adx(highs[:-1], lows[:-1], closes[:-1], period=14) if len(closes) >= 51 else float(adx_val)
    adx_rising = float(adx_val) > float(adx_prev)
    adx_falling = float(adx_val) < float(adx_prev)
    recent_high_20 = max(highs[-20:])
    recent_low_20 = min(lows[-20:])
    prior_high_20 = max(highs[-21:-1]) if len(highs) >= 21 else recent_high_20
    prior_low_20 = min(lows[-21:-1]) if len(lows) >= 21 else recent_low_20
    near_20bar_high = (recent_high_20 - current_close) / max(abs(current_close), 1e-9) * 100.0 <= 1.0
    near_20bar_low = (current_close - recent_low_20) / max(abs(current_close), 1e-9) * 100.0 <= 1.0
    price_near_upper_bb = 0 < (float(bb_upper) - current_close) / max(abs(current_close), 1e-9) < 0.01
    price_near_lower_bb = 0 < (current_close - float(bb_lower)) / max(abs(current_close), 1e-9) < 0.01
    prior_price_window = closes[-21:-1] if len(closes) >= 21 else closes[:-1]
    prior_min_close = min(prior_price_window) if prior_price_window else current_close
    prior_max_close = max(prior_price_window) if prior_price_window else current_close
    if precomputed_indicators and precomputed_indicators.get('rsi_window'):
        _rsi_win = precomputed_indicators['rsi_window']
        prior_min_rsi = min(_rsi_win[:-1]) if len(_rsi_win) > 1 else float(rsi_val)
        prior_max_rsi = max(_rsi_win[:-1]) if len(_rsi_win) > 1 else float(rsi_val)
    else:
        _rsi_series = [rsi(closes[:i]) for i in range(max(15, len(closes) - 20), len(closes) + 1)]
        _prior_rsi_window = _rsi_series[:-1]
        prior_min_rsi = min(_prior_rsi_window) if _prior_rsi_window else float(rsi_val)
        prior_max_rsi = max(_prior_rsi_window) if _prior_rsi_window else float(rsi_val)
    if precomputed_indicators and precomputed_indicators.get('macd_line_window'):
        _macd_win = precomputed_indicators['macd_line_window']
        prior_min_macd = min(_macd_win[:-1]) if len(_macd_win) > 1 else float(macd_line_val)
        prior_max_macd = max(_macd_win[:-1]) if len(_macd_win) > 1 else float(macd_line_val)
    else:
        _macd_series = [macd(closes[:i])[0] for i in range(max(27, len(closes) - 20), len(closes) + 1)]
        _prior_macd_window = _macd_series[:-1]
        prior_min_macd = min(_prior_macd_window) if _prior_macd_window else float(macd_line_val)
        prior_max_macd = max(_prior_macd_window) if _prior_macd_window else float(macd_line_val)
    rsi_bullish_divergence = current_close < prior_min_close and float(rsi_val) > prior_min_rsi + 3.0
    rsi_bearish_divergence = current_close > prior_max_close and float(rsi_val) < prior_max_rsi - 3.0
    macd_bullish_divergence = current_close < prior_min_close and float(macd_line_val) > prior_min_macd
    macd_bearish_divergence = current_close > prior_max_close and float(macd_line_val) < prior_max_macd
    four_hour_closes = closes[::4]
    if len(four_hour_closes) >= 50:
        htf_e21 = ema(four_hour_closes, 21)[-1]
        htf_e50 = ema(four_hour_closes, 50)[-1]
        htf_4h_bull_trend = four_hour_closes[-1] > htf_e21 > htf_e50
        htf_4h_bear_trend = four_hour_closes[-1] < htf_e21 < htf_e50
    else:
        htf_4h_bull_trend = False
        htf_4h_bear_trend = False

    return Snapshot(
        close=float(closes[-1]),
        open=float(current_open),
        high=float(current_high),
        low=float(current_low),
        prev_open=float(prev_open),
        prev_high=float(prev_high),
        prev_low=float(prev_low),
        prev_close=float(prev_close),
        rsi=float(rsi_val),
        e9=float(e9),
        e21=float(e21),
        e50=float(e50),
        e200=float(e200),
        e9_prev=float(e9_prev),
        e21_prev=float(e21_prev),
        e50_prev=float(e50_prev),
        e200_prev=float(e200_prev),
        above_ema200=bool(above_e200),
        below_ema200=bool(below_e200),
        macd_line=float(macd_line_val),
        macd_signal=float(macd_signal_val),
        macd_hist=float(macd_hist_val),
        macd_hist_prev=float(macd_hist_prev),
        adx=float(adx_val),
        vol_ratio=float(vol_r),
        bb_mid=float(bb_mid),
        bb_upper=float(bb_upper),
        bb_lower=float(bb_lower),
        bb_bandwidth=float(bb_bw),
        bb_squeeze=bool(bb_squeeze),
        price_near_upper_bb=bool(price_near_upper_bb),
        price_near_lower_bb=bool(price_near_lower_bb),
        higher_highs=bool(higher_highs),
        lower_lows=bool(lower_lows),
        bullish_engulfing=bool(bullish_engulfing),
        bearish_engulfing=bool(bearish_engulfing),
        inside_bar=bool(inside_bar),
        outside_bar=bool(outside_bar),
        pin_bar_bull=bool(pin_bar_bull),
        pin_bar_bear=bool(pin_bar_bear),
        three_green_candles=bool(three_green_candles),
        three_red_candles=bool(three_red_candles),
        atr20=float(atr20),
        atr20_prev=float(atr20_prev),
        atr_percentile_high=bool(atr_percentile_high),
        atr_expansion=bool(atr_expansion),
        candle_range_above_atr=bool(candle_range_above_atr),
        two_expansion_green_candles=bool(
            current_close > current_open
            and prev_close > prev_open
            and current_range_atr >= 1.2
            and prev_range_atr >= 1.2
        ),
        two_expansion_red_candles=bool(
            current_close < current_open
            and prev_close < prev_open
            and current_range_atr >= 1.2
            and prev_range_atr >= 1.2
        ),
        ema21_rising=bool(ema21_rising),
        ema21_falling=bool(ema21_falling),
        ema_fan_wide=bool(ema_fan_wide),
        strong_ema21_slope_up=bool(ema21_slope_pct >= 0.15),
        strong_ema21_slope_down=bool(ema21_slope_pct <= -0.15),
        adx_rising=bool(adx_rising),
        adx_falling=bool(adx_falling),
        near_20bar_high=bool(near_20bar_high),
        near_20bar_low=bool(near_20bar_low),
        breaks_20bar_high=bool(current_close > prior_high_20),
        breaks_20bar_low=bool(current_close < prior_low_20),
        rsi_bullish_divergence=bool(rsi_bullish_divergence),
        rsi_bearish_divergence=bool(rsi_bearish_divergence),
        macd_bullish_divergence=bool(macd_bullish_divergence),
        macd_bearish_divergence=bool(macd_bearish_divergence),
        htf_4h_bull_trend=bool(htf_4h_bull_trend),
        htf_4h_bear_trend=bool(htf_4h_bear_trend),
        hour_utc=int(time_for_filter.hour),
        symbol=symbol,
    )
