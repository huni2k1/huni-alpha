#!/usr/bin/env python3
"""
Statistical setup analyzer for the trading bot.

Measures which indicator conditions and combinations have historical edge
using the bot's existing candle data, indicators, next-open entry model,
and ATR-based TP/SL logic.

Key design decisions:
  1. Single flat condition list — conditions are factual statements about
     the candle and are tested against both long and short outcomes.
  2. Non-overlapping trade selection at evaluation time so the selected
     outcomes are less correlated and the statistical tests are stricter.
  3. Baselines use the same matched sampling logic as the tested subset.
  4. Walk-forward validation can include per-symbol breakdown for any
     setup that survives out-of-sample filters.
  5. Runtime-sensitive row building stays optimized with precomputed
     indicators, pinned end dates, and optional parallel workers.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Callable, Optional

import numpy as np

from .backtester import fetch_klines_historical_cached
from .core.indicators import ema, market_structure, rsi
from .core.types import Direction, MarketRegime, Template
from .regime import VALID_REGIMES, build_regime_lookup
from .setup_conditions import ALL_CONDITIONS, matches_conditions, normalize_conditions
from .signals.snapshot import precompute_indicators_for_all_candles
from .statistical_utils import (
    SampleStats,
    benjamini_hochberg_adjusted,
    build_walk_forward_windows,
    summarize_outcomes,
    two_proportion_ztest,
)


log = logging.getLogger("setup_analyzer")
if not log.handlers:
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(handler)

from .signals.config import SYMBOLS as _CFG_SYMBOLS

DEFAULT_SYMBOLS = list(_CFG_SYMBOLS) or [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "DOTUSDT",
]
DEFAULT_REPORT_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_analysis_report.json")
DEFAULT_VALIDATED_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "validated_setups.json")
DEFAULT_CANDIDATE_DATASET_OUTPUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "candidate_research_dataset.csv",
)
DEFAULT_WARMUP_CANDLES = 1000
DEFAULT_MAX_HOLDING_CANDLES = 180
DEFAULT_MIN_TRAIN_TRADES = 30
DEFAULT_MIN_TEST_TRADES = 15
DEFAULT_PREFILTER_P = 0.10
DEFAULT_VALIDATION_P = 0.05
DEFAULT_FEE_PCT = 0.10
DEFAULT_SLIPPAGE_PCT = 0.05
MAX_COMBO_TESTS_DEFAULT = 50
MAX_CANDIDATE_CONDITIONS = 8
EVAL_COOLDOWN_CANDLES = 48
DEFAULT_DISCOVERY_VARIANTS = ("pooled",)
VALID_DISCOVERY_VARIANTS = ("pooled", "symbol_specific", "regime")
# Module-level interval; overridable via --interval on the CLI for HTF mining
INTERVAL = "1h"
# Cross-symbol context for new condition families. Populated by analyze_symbol_universe
# before per-symbol row building.
BTC_PCT_4H_BY_CLOSE_MS: dict[int, float] = {}
FUNDING_RATE_BY_TIME_MS: dict[str, dict[int, float]] = {}
REGIME_BTC_SYMBOL = "BTCUSDT"
DOMINANCE_THRESHOLDS = {
    "train_avg_pnl_pct": 0.15,
    "test_avg_pnl_pct": 0.15,
    "train_profit_factor": 0.15,
    "test_profit_factor": 0.15,
    "test_edge_win_rate": 3.0,
}

# Groups of conditions that are nested or highly correlated.
# During combo candidate selection only the best-ranked condition per family
# is forwarded, preventing the combos from testing the same filter twice.
CONDITION_FAMILIES: list[frozenset] = [
    frozenset({"rsi_below_28", "rsi_below_30", "rsi_below_32", "rsi_below_40"}),
    frozenset({"rsi_above_60", "rsi_above_70"}),
    frozenset({"rsi_35_to_50", "rsi_40_to_60"}),
    frozenset({"adx_above_25", "adx_above_30"}),
    frozenset({"adx_below_20", "adx_below_25"}),
    frozenset({"vol_above_1_2", "vol_above_1_5", "vol_above_2_0"}),
    frozenset({"ema21_rising", "ema_fan_wide", "strong_ema21_slope_up"}),
    frozenset({"atr_percentile_high", "atr_expansion"}),
    frozenset({"macd_hist_rising", "macd_cross_up"}),
    frozenset({"macd_hist_falling", "macd_cross_down"}),
    frozenset({"macd_line_above_signal", "macd_hist_positive"}),
    frozenset({"macd_line_below_signal", "macd_hist_negative"}),
    frozenset({"rsi_bullish_divergence", "macd_bullish_divergence"}),
    frozenset({"rsi_bearish_divergence", "macd_bearish_divergence"}),
    frozenset({"taker_buy_ratio_above_0_55", "taker_buy_ratio_above_0_60"}),
    frozenset({"taker_buy_ratio_below_0_45", "taker_buy_ratio_below_0_40"}),
    frozenset({"funding_above_0_01", "funding_above_0_05"}),
    frozenset({"funding_below_neg_0_01", "funding_below_neg_0_05"}),
]

SETUP_TEMPLATES = {
    "standard": {"sl_atr_mult": 1.5, "rr_ratio": 2.0},
    "wide": {"sl_atr_mult": 2.0, "rr_ratio": 2.5},
}
LEGACY_TEMPLATE_ALIASES = {
    "trend": "standard",
    "breakout": "wide",
}


def _normalize_template_name(template_name: str) -> str:
    """Map legacy template names onto the current runtime terminology."""
    return LEGACY_TEMPLATE_ALIASES.get(str(template_name), str(template_name))
def candles_to_ohlcv_lists(candles: list[dict]) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    """Convert candle dicts into parallel OHLCV lists."""
    opens = [float(c["open"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]
    return opens, highs, lows, closes, volumes


def calculate_atr(candles: list[dict], period: int = 20) -> float:
    """ATR matching the scanner's TP/SL logic."""
    if len(candles) < 2:
        return 0.0

    _, highs, lows, closes, _ = candles_to_ohlcv_lists(candles)
    tr_values = []
    for i in range(1, len(closes)):
        tr_values.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    if not tr_values:
        return 0.0
    return sum(tr_values[-period:]) / min(period, len(tr_values))


def _build_adx_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float]:
    """Build a per-candle ADX series using the scanner's Wilder-style approach."""
    series = [0.0] * len(closes)
    if len(highs) < period * 2:
        return series

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(highs)):
        h_diff = highs[i] - highs[i - 1]
        l_diff = lows[i - 1] - lows[i]
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0.0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0.0)
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    def wilder(values: list[float], p: int) -> list[float]:
        result = [sum(values[:p])]
        for value in values[p:]:
            result.append(result[-1] - result[-1] / p + value)
        return result

    atr_s = wilder(trs, period)
    pdm_s = wilder(plus_dm, period)
    mdm_s = wilder(minus_dm, period)
    dx_values = []
    for atr_val, plus_val, minus_val in zip(atr_s, pdm_s, mdm_s):
        if atr_val == 0:
            dx_values.append(0.0)
            continue
        pdi = 100.0 * plus_val / atr_val
        mdi = 100.0 * minus_val / atr_val
        dx_values.append(100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0)

    start_idx = period
    for offset, candle_idx in enumerate(range(start_idx, len(closes))):
        window = dx_values[max(0, offset - period + 1):offset + 1]
        series[candle_idx] = float(sum(window) / len(window)) if window else 0.0
    return series


def _build_bollinger_context(
    closes: list[float],
    period: int = 20,
    std_mult: float = 2.0,
    squeeze_lookback: int = 100,
) -> dict:
    """Precompute per-candle Bollinger values and squeeze flags."""
    mids = [0.0] * len(closes)
    uppers = [0.0] * len(closes)
    lowers = [0.0] * len(closes)
    bandwidths = [5.0] * len(closes)
    squeezes = [False] * len(closes)

    for idx in range(period - 1, len(closes)):
        window = closes[idx - period + 1:idx + 1]
        mid = float(sum(window) / len(window))
        std = float(np.std(window))
        upper = mid + std_mult * std
        lower = mid - std_mult * std
        bw = (upper - lower) / mid * 100.0 if mid > 0 else 5.0
        mids[idx] = mid
        uppers[idx] = upper
        lowers[idx] = lower
        bandwidths[idx] = bw
        history = [value for value in bandwidths[max(period - 1, idx - squeeze_lookback):idx] if value is not None]
        if history:
            squeezes[idx] = bw < float(np.percentile(history, 20))

    return {
        "bb_mid": mids,
        "bb_upper": uppers,
        "bb_lower": lowers,
        "bb_bandwidth": bandwidths,
        "bb_squeeze": squeezes,
    }


def _build_volume_ratio_series(volumes: list[float], period: int = 20) -> list[float]:
    """Precompute volume ratio for every candle."""
    series = [1.0] * len(volumes)
    for idx in range(period, len(volumes)):
        prior = volumes[idx - period:idx]
        avg = sum(prior) / len(prior) if prior else 0.0
        series[idx] = volumes[idx] / avg if avg > 0 else 1.0
    return series


def _build_atr_series(candles: list[dict], period: int = 20) -> list[float]:
    """Precompute ATR values for every candle."""
    series = [0.0] * len(candles)
    if len(candles) < 2:
        return series
    for idx in range(1, len(candles)):
        series[idx] = float(calculate_atr(candles[:idx + 1], period))
    return series


def _build_4h_context(candles: list[dict]) -> dict:
    """
    Build a proper 4h close series from 1h candles using UTC timestamp boundaries.

    Each UTC 4h period (00-04, 04-08, ..., 20-24) is treated as one bar.
    The close of a 4h bar is the close of the *last 1h candle that starts
    within that period*.  Incomplete periods (the current in-progress 4h
    bar) are excluded so there is no look-ahead.

    Returns:
        closes       -- list of completed 4h bar closes
        count_by_idx -- for each 1h candle index, how many completed 4h
                        closes are available as of that candle
    """
    closes_series: list[float] = []
    count_by_idx: list[int] = []
    current_4h_boundary: Optional[datetime] = None
    current_4h_close: Optional[float] = None
    completed = 0

    for candle in candles:
        ts = datetime.fromtimestamp(int(candle["open_time"]) / 1000, tz=timezone.utc)
        boundary = ts.replace(hour=(ts.hour // 4) * 4, minute=0, second=0, microsecond=0)

        if boundary != current_4h_boundary:
            # A new 4h period has started — seal the previous one
            if current_4h_close is not None:
                closes_series.append(current_4h_close)
                completed += 1
            current_4h_boundary = boundary

        current_4h_close = float(candle["close"])
        count_by_idx.append(completed)

    return {"closes": closes_series, "count_by_idx": count_by_idx}


def precompute_symbol_context(candles: list[dict]) -> dict:
    """Precompute reusable indicator context for one symbol."""
    ohlcv = [[c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in candles]
    _, highs, lows, closes, volumes = candles_to_ohlcv_lists(candles)
    cheap = precompute_indicators_for_all_candles(ohlcv)
    return {
        "ohlcv": ohlcv,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "volumes": volumes,
        "cheap": cheap,
        "adx": _build_adx_series(highs, lows, closes),
        "atr20": _build_atr_series(candles, 20),
        "vol_ratio": _build_volume_ratio_series(volumes),
        "bb": _build_bollinger_context(closes),
        "htf_4h": _build_4h_context(candles),
    }


def compute_indicator_snapshot(symbol: str, candles: list[dict], idx: int, context: Optional[dict] = None) -> dict:
    """Compute all analyzer indicators for the candle at idx using history up to idx."""
    window = candles[:idx + 1]
    if context is None:
        context = precompute_symbol_context(candles)

    cheap = context["cheap"].get(idx, {})
    cheap_prev = context["cheap"].get(idx - 1, {}) if idx > 0 else {}
    closes = context["closes"][:idx + 1]
    highs = context["highs"][:idx + 1]
    lows = context["lows"][:idx + 1]
    bb_ctx = context["bb"]
    ohlcv_window = context["ohlcv"][max(0, idx - 5):idx + 1]
    higher_highs, lower_lows = market_structure(ohlcv_window)
    candle_time = datetime.fromtimestamp(window[-1]["close_time"] / 1000, tz=timezone.utc)
    candle = window[-1]
    prev_candle = window[-2] if len(window) >= 2 else candle
    atr20 = float(context["atr20"][idx])
    atr20_prev = float(context["atr20"][idx - 1]) if idx > 0 else 0.0
    current_open = float(candle["open"])
    current_high = float(candle["high"])
    current_low = float(candle["low"])
    current_close = float(candle["close"])
    prev_open = float(prev_candle["open"])
    prev_high = float(prev_candle["high"])
    prev_low = float(prev_candle["low"])
    prev_close = float(prev_candle["close"])
    current_body = abs(current_close - current_open)
    current_range = max(current_high - current_low, 1e-9)
    upper_wick = current_high - max(current_open, current_close)
    lower_wick = min(current_open, current_close) - current_low
    bullish_engulfing = (
        len(window) >= 2
        and current_close > current_open
        and prev_close < prev_open
        and current_open <= prev_close
        and current_close >= prev_open
    )
    bearish_engulfing = (
        len(window) >= 2
        and current_close < current_open
        and prev_close > prev_open
        and current_open >= prev_close
        and current_close <= prev_open
    )
    inside_bar = len(window) >= 2 and current_high <= prev_high and current_low >= prev_low
    outside_bar = len(window) >= 2 and current_high >= prev_high and current_low <= prev_low
    pin_bar_bull = lower_wick >= current_body * 2.0 and upper_wick <= current_body and current_close >= current_open
    pin_bar_bear = upper_wick >= current_body * 2.0 and lower_wick <= current_body and current_close <= current_open
    three_green_candles = (
        idx >= 2
        and all(float(candles[i]["close"]) > float(candles[i]["open"]) for i in range(idx - 2, idx + 1))
    )
    three_red_candles = (
        idx >= 2
        and all(float(candles[i]["close"]) < float(candles[i]["open"]) for i in range(idx - 2, idx + 1))
    )
    ema_fan_pct = abs(float(cheap.get("e9") or 0.0) - float(cheap.get("e50") or 0.0)) / max(current_close, 1e-9) * 100.0
    adx_prev = float(context["adx"][idx - 1]) if idx > 0 else 0.0
    atr_window = [float(v) for v in context["atr20"][max(0, idx - 99):idx] if float(v) > 0]
    atr_percentile_high = bool(atr_window) and atr20 >= float(np.percentile(atr_window, 80))
    atr_expansion = atr20_prev > 0 and atr20 >= atr20_prev * 1.2
    candle_range_atr = current_range / atr20 if atr20 > 0 else 0.0
    prev_range = max(prev_high - prev_low, 0.0)
    prev_range_atr = prev_range / atr20_prev if atr20_prev > 0 else 0.0
    recent_high_20 = max(highs[max(0, idx - 19):idx + 1]) if highs else current_high
    recent_low_20 = min(lows[max(0, idx - 19):idx + 1]) if lows else current_low
    prior_high_20 = max(highs[max(0, idx - 20):idx]) if idx > 0 else current_high
    prior_low_20 = min(lows[max(0, idx - 20):idx]) if idx > 0 else current_low
    distance_from_20bar_high_pct = (recent_high_20 - current_close) / max(current_close, 1e-9) * 100.0
    distance_from_20bar_low_pct = (current_close - recent_low_20) / max(current_close, 1e-9) * 100.0
    ema21_slope_pct = ((float(cheap.get("e21") or 0.0) - float(cheap_prev.get("e21") or 0.0)) / max(current_close, 1e-9)) * 100.0
    rsi_series = [float(context["cheap"].get(i, {}).get("rsi", 50.0)) for i in range(max(0, idx - 20), idx + 1)]
    macd_series = [float(context["cheap"].get(i, {}).get("macd_line", 0.0)) for i in range(max(0, idx - 20), idx + 1)]
    price_window = closes[max(0, len(closes) - 21):]
    prior_price_window = price_window[:-1]
    prior_rsi_window = rsi_series[:-1]
    prior_macd_window = macd_series[:-1]
    prior_min_close = min(prior_price_window) if prior_price_window else current_close
    prior_max_close = max(prior_price_window) if prior_price_window else current_close
    prior_min_rsi = min(prior_rsi_window) if prior_rsi_window else float(cheap.get("rsi", 50.0))
    prior_max_rsi = max(prior_rsi_window) if prior_rsi_window else float(cheap.get("rsi", 50.0))
    prior_min_macd = min(prior_macd_window) if prior_macd_window else float(cheap.get("macd_line", 0.0))
    prior_max_macd = max(prior_macd_window) if prior_macd_window else float(cheap.get("macd_line", 0.0))
    rsi_bullish_divergence = current_close < prior_min_close and float(cheap.get("rsi", 50.0)) > prior_min_rsi + 3.0
    rsi_bearish_divergence = current_close > prior_max_close and float(cheap.get("rsi", 50.0)) < prior_max_rsi - 3.0
    macd_bullish_divergence = current_close < prior_min_close and float(cheap.get("macd_line", 0.0)) > prior_min_macd
    macd_bearish_divergence = current_close > prior_max_close and float(cheap.get("macd_line", 0.0)) < prior_max_macd
    htf_4h_ctx = context.get("htf_4h", {})
    htf_4h_n = htf_4h_ctx["count_by_idx"][idx] if htf_4h_ctx.get("count_by_idx") else 0
    four_hour_closes = htf_4h_ctx["closes"][:htf_4h_n] if htf_4h_ctx.get("closes") else []
    if len(four_hour_closes) >= 50:
        htf_e21 = ema(four_hour_closes, 21)[-1]
        htf_e50 = ema(four_hour_closes, 50)[-1]
        htf_4h_bull_trend = four_hour_closes[-1] > htf_e21 > htf_e50
        htf_4h_bear_trend = four_hour_closes[-1] < htf_e21 < htf_e50
    else:
        htf_4h_bull_trend = False
        htf_4h_bear_trend = False

    return {
        "symbol": symbol,
        "idx": idx,
        "open_time": int(window[-1]["open_time"]),
        "close_time": int(window[-1]["close_time"]),
        "month": candle_time.strftime("%Y-%m"),
        "hour_utc": candle_time.hour,
        "open": current_open,
        "high": current_high,
        "low": current_low,
        "close": current_close,
        "volume": float(window[-1]["volume"]),
        "prev_open": prev_open,
        "prev_high": prev_high,
        "prev_low": prev_low,
        "prev_close": prev_close,
        "rsi": float(cheap.get("rsi", rsi(closes))),
        "e9": float(cheap.get("e9") or 0.0),
        "e21": float(cheap.get("e21") or 0.0),
        "e50": float(cheap.get("e50") or 0.0),
        "e200": float(cheap.get("e200") or 0.0),
        "e9_prev": float(cheap_prev.get("e9") or 0.0),
        "e21_prev": float(cheap_prev.get("e21") or 0.0),
        "e50_prev": float(cheap_prev.get("e50") or 0.0),
        "e200_prev": float(cheap_prev.get("e200") or 0.0),
        "bull_ema_align": bool(cheap.get("bull_align", False)),
        "bear_ema_align": bool(cheap.get("bear_align", False)),
        "above_ema200": bool(cheap.get("above_e200", False)),
        "below_ema200": bool(cheap.get("below_e200", False)),
        "macd_line": float(cheap.get("macd_line", 0.0)),
        "macd_signal": float(cheap.get("macd_signal", 0.0)),
        "macd_hist": float(cheap.get("macd_hist", 0.0)),
        "macd_hist_prev": float(cheap.get("macd_hist_prev", 0.0)),
        "adx": float(context["adx"][idx]),
        "vol_ratio": float(context["vol_ratio"][idx]),
        "bb_mid": float(bb_ctx["bb_mid"][idx]),
        "bb_upper": float(bb_ctx["bb_upper"][idx]),
        "bb_lower": float(bb_ctx["bb_lower"][idx]),
        "bb_bandwidth": float(bb_ctx["bb_bandwidth"][idx]),
        "bb_squeeze": bool(bb_ctx["bb_squeeze"][idx]),
        "higher_highs": bool(higher_highs),
        "lower_lows": bool(lower_lows),
        "atr20": atr20,
        "atr20_prev": atr20_prev,
        "bullish_engulfing": bullish_engulfing,
        "bearish_engulfing": bearish_engulfing,
        "inside_bar": inside_bar,
        "outside_bar": outside_bar,
        "pin_bar_bull": pin_bar_bull,
        "pin_bar_bear": pin_bar_bear,
        "three_green_candles": three_green_candles,
        "three_red_candles": three_red_candles,
        "ema21_rising": float(cheap.get("e21") or 0.0) > float(cheap_prev.get("e21") or 0.0),
        "ema21_falling": float(cheap.get("e21") or 0.0) < float(cheap_prev.get("e21") or 0.0),
        "ema_fan_wide": ema_fan_pct >= 1.0,
        "ema_fan_pct": ema_fan_pct,
        "adx_prev": adx_prev,
        "adx_rising": float(context["adx"][idx]) > adx_prev,
        "adx_falling": float(context["adx"][idx]) < adx_prev,
        "atr_percentile_high": atr_percentile_high,
        "atr_expansion": atr_expansion,
        "candle_range_atr": candle_range_atr,
        "candle_range_above_atr": candle_range_atr >= 1.2,
        "two_expansion_green_candles": (
            idx >= 1
            and current_close > current_open
            and prev_close > prev_open
            and candle_range_atr >= 1.2
            and prev_range_atr >= 1.2
        ),
        "two_expansion_red_candles": (
            idx >= 1
            and current_close < current_open
            and prev_close < prev_open
            and candle_range_atr >= 1.2
            and prev_range_atr >= 1.2
        ),
        "strong_ema21_slope_up": ema21_slope_pct >= 0.15,
        "strong_ema21_slope_down": ema21_slope_pct <= -0.15,
        "recent_high_20": float(recent_high_20),
        "recent_low_20": float(recent_low_20),
        "distance_from_20bar_high_pct": distance_from_20bar_high_pct,
        "distance_from_20bar_low_pct": distance_from_20bar_low_pct,
        "near_20bar_high": distance_from_20bar_high_pct <= 1.0,
        "near_20bar_low": distance_from_20bar_low_pct <= 1.0,
        "breaks_20bar_high": idx > 0 and current_close > prior_high_20,
        "breaks_20bar_low": idx > 0 and current_close < prior_low_20,
        "rsi_bullish_divergence": rsi_bullish_divergence,
        "rsi_bearish_divergence": rsi_bearish_divergence,
        "macd_bullish_divergence": macd_bullish_divergence,
        "macd_bearish_divergence": macd_bearish_divergence,
        "htf_4h_bull_trend": htf_4h_bull_trend,
        "htf_4h_bear_trend": htf_4h_bear_trend,
        # Taker buy aggressiveness: ratio of taker-buys to total quote volume on this candle
        "taker_buy_ratio": (
            float(candle.get("taker_buy_quote_asset_volume", 0.0))
            / max(float(candle.get("quote_asset_volume", 0.0)), 1e-9)
            if candle.get("quote_asset_volume", 0.0) > 0 else 0.5
        ),
        # BTC contagion: BTC's last-4-candle pct change at this same close_time
        "btc_pct_4h": float(BTC_PCT_4H_BY_CLOSE_MS.get(int(candle["close_time"]), 0.0)),
        # Funding rate at this candle's close_time (Binance Futures, hourly forward-fill)
        "funding_rate": float(FUNDING_RATE_BY_TIME_MS.get(symbol, {}).get(int(candle["close_time"]), 0.0)),
    }


def _populate_btc_and_funding_context(symbols: list[str], start_ms: int, end_ms: int) -> None:
    """Populate module globals BTC_PCT_4H_BY_CLOSE_MS and FUNDING_RATE_BY_TIME_MS."""
    BTC_PCT_4H_BY_CLOSE_MS.clear()
    FUNDING_RATE_BY_TIME_MS.clear()

    # BTC contagion: 4-hour rolling pct change keyed by candle close_time
    try:
        btc_candles = fetch_klines_historical_cached("BTCUSDT", INTERVAL, start_ms, end_ms, use_cache=True)
        if btc_candles and len(btc_candles) > 5:
            for i in range(4, len(btc_candles)):
                close_now = float(btc_candles[i]["close"])
                close_4 = float(btc_candles[i - 4]["close"])
                if close_4 > 0:
                    BTC_PCT_4H_BY_CLOSE_MS[int(btc_candles[i]["close_time"])] = (close_now - close_4) / close_4 * 100.0
            log.info(f"BTC contagion context populated for {len(BTC_PCT_4H_BY_CLOSE_MS)} candles")
    except Exception as exc:
        log.warning(f"BTC contagion context unavailable: {exc}")

    # Funding rates: forward-fill 8-hour funding events onto each candle's close_time
    for symbol in symbols:
        try:
            FUNDING_RATE_BY_TIME_MS[symbol] = _fetch_funding_series(symbol, start_ms, end_ms)
        except Exception as exc:
            log.warning(f"  {symbol}: funding rate unavailable: {exc}")
            FUNDING_RATE_BY_TIME_MS[symbol] = {}
    populated = sum(1 for v in FUNDING_RATE_BY_TIME_MS.values() if v)
    log.info(f"Funding rate context populated for {populated}/{len(symbols)} symbols")


def _fetch_funding_series(symbol: str, start_ms: int, end_ms: int) -> dict[int, float]:
    """Fetch all Binance Futures funding events between start_ms and end_ms, forward-filled by hour."""
    import urllib.request, urllib.parse, json as _json
    out: dict[int, float] = {}
    cursor = start_ms
    events: list[tuple[int, float]] = []  # (fundingTime_ms, fundingRate)
    while cursor < end_ms:
        params = {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000}
        url = "https://fapi.binance.com/fapi/v1/fundingRate?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = _json.loads(resp.read())
        except Exception:
            break
        if not data:
            break
        for row in data:
            events.append((int(row["fundingTime"]), float(row["fundingRate"])))
        cursor = int(data[-1]["fundingTime"]) + 1
        if len(data) < 1000:
            break
    # Forward-fill: every hour in [start_ms, end_ms] takes the most recent funding event's rate
    if not events:
        return {}
    events.sort()
    cur_idx = 0
    cur_rate = events[0][1]
    t = start_ms
    while t < end_ms:
        while cur_idx + 1 < len(events) and events[cur_idx + 1][0] <= t:
            cur_idx += 1
            cur_rate = events[cur_idx][1]
        # Key by approximate candle close_time (open + 3600_000 - 1) — match snapshot's lookup
        out[t + 3_600_000 - 1] = cur_rate
        t += 3_600_000
    return out


def load_symbol_universe_rows(
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    analysis_start_ms: int,
    warmup_candles: int,
    max_holding_candles: int,
    fee_pct: float,
    slippage_pct: float,
    workers: int = 1,
) -> tuple[list[dict], dict[str, list[dict]], dict[str, list[dict]]]:
    """Load candle history and analyzer rows for the symbol universe."""
    all_rows = []
    rows_by_symbol: dict[str, list[dict]] = {}
    candles_by_symbol: dict[str, list[dict]] = {}
    worker_count = max(1, workers)

    if worker_count == 1 or len(symbols) == 1:
        for symbol in symbols:
            log.info(f"Loading {symbol} candles for statistical analysis...")
            candles = fetch_klines_historical_cached(symbol, INTERVAL, start_ms, end_ms, use_cache=True)
            if not candles or len(candles) < warmup_candles + 100:
                log.warning(f"  {symbol}: insufficient candles ({len(candles) if candles else 0}), skipping")
                rows_by_symbol[symbol] = []
                candles_by_symbol[symbol] = candles or []
                continue
            rows = build_symbol_rows(
                symbol=symbol,
                candles=candles,
                analysis_start_ms=analysis_start_ms,
                warmup_candles=warmup_candles,
                max_holding_candles=max_holding_candles,
                fee_pct=fee_pct,
                slippage_pct=slippage_pct,
            )
            log.info(f"  {symbol}: built {len(rows)} study rows")
            candles_by_symbol[symbol] = candles
            rows_by_symbol[symbol] = rows
            all_rows.extend(rows)
        return all_rows, rows_by_symbol, candles_by_symbol

    log.info("Building study rows with %d workers...", worker_count)
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                load_and_build_symbol_rows,
                symbol,
                start_ms,
                end_ms,
                analysis_start_ms,
                warmup_candles,
                max_holding_candles,
                fee_pct,
                slippage_pct,
            ): symbol
            for symbol in symbols
        }
        for future in as_completed(future_map):
            loaded_symbol, rows = future.result()
            log.info(f"  {loaded_symbol}: built {len(rows)} study rows")
            rows_by_symbol[loaded_symbol] = rows
            all_rows.extend(rows)

    for symbol in symbols:
        candles = fetch_klines_historical_cached(symbol, INTERVAL, start_ms, end_ms, use_cache=True) or []
        candles_by_symbol[symbol] = candles
        rows_by_symbol.setdefault(symbol, [])
    return all_rows, rows_by_symbol, candles_by_symbol


# Same-candle TP/SL resolution lives in execution/fills.py; research uses the
# deterministic tie-break ("sl") so reruns stay bit-for-bit reproducible.
from .execution.fills import resolve_same_candle_hit as _resolve_same_candle_hit_impl


def resolve_same_candle_hit(candle: dict, tp_price: float, sl_price: float) -> str:
    return _resolve_same_candle_hit_impl(candle, tp_price, sl_price, tie_break="sl")


def simulate_forward_trade(
    candles: list[dict],
    signal_idx: int,
    direction: Direction,
    sl_atr_mult: float,
    rr_ratio: float,
    max_holding_candles: int = DEFAULT_MAX_HOLDING_CANDLES,
    fee_pct: float = DEFAULT_FEE_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> Optional[dict]:
    """Simulate a next-open entry forward until TP, SL, or timeout."""
    entry_idx = signal_idx + 1
    if entry_idx >= len(candles):
        return None

    atr = calculate_atr(candles[:signal_idx + 1], 20)
    if atr <= 0:
        return None

    entry_candle = candles[entry_idx]
    entry_price = float(entry_candle["open"])
    if slippage_pct > 0:
        if direction == "LONG":
            entry_price *= 1.0 + slippage_pct / 100.0
        else:
            entry_price *= 1.0 - slippage_pct / 100.0

    sl_distance = atr * sl_atr_mult
    tp_distance = sl_distance * rr_ratio
    if direction == "LONG":
        tp_price = entry_price + tp_distance
        sl_price = entry_price - sl_distance
    else:
        tp_price = entry_price - tp_distance
        sl_price = entry_price + sl_distance

    last_idx = min(len(candles) - 1, entry_idx + max_holding_candles - 1)
    exit_reason = "TIMEOUT"
    exit_idx = last_idx
    exit_price = float(candles[last_idx]["close"])

    for idx in range(entry_idx, last_idx + 1):
        candle = candles[idx]
        high = float(candle["high"])
        low = float(candle["low"])
        if direction == "LONG":
            tp_hit = high >= tp_price
            sl_hit = low <= sl_price
        else:
            tp_hit = low <= tp_price
            sl_hit = high >= sl_price

        if tp_hit and sl_hit:
            exit_reason = resolve_same_candle_hit(candle, tp_price, sl_price)
            exit_idx = idx
            exit_price = tp_price if exit_reason == "TP" else sl_price
            break
        if tp_hit:
            exit_reason = "TP"
            exit_idx = idx
            exit_price = tp_price
            break
        if sl_hit:
            exit_reason = "SL"
            exit_idx = idx
            exit_price = sl_price
            break

    if slippage_pct > 0:
        if direction == "LONG":
            exit_price *= 1.0 - slippage_pct / 100.0
        else:
            exit_price *= 1.0 + slippage_pct / 100.0

    if direction == "LONG":
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
    else:
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100.0
    pnl_pct -= fee_pct

    return {
        "is_win": exit_reason == "TP",
        "exit_reason": exit_reason,
        "entry_idx": entry_idx,
        "exit_idx": exit_idx,
        "holding_candles": (exit_idx - entry_idx) + 1,
        "entry_price": round(entry_price, 8),
        "exit_price": round(exit_price, 8),
        "tp_price": round(tp_price, 8),
        "sl_price": round(sl_price, 8),
        "atr": round(atr, 8),
        "pnl_pct": round(pnl_pct, 4),
    }


def _outcome_key(template_name: str, direction: str) -> str:
    return f"{_normalize_template_name(template_name)}_{direction.lower()}"


def build_symbol_rows(
    symbol: str,
    candles: list[dict],
    analysis_start_ms: int,
    warmup_candles: int = DEFAULT_WARMUP_CANDLES,
    max_holding_candles: int = DEFAULT_MAX_HOLDING_CANDLES,
    fee_pct: float = DEFAULT_FEE_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> list[dict]:
    """Build analyzer rows for one symbol."""
    rows = []
    context = precompute_symbol_context(candles)
    start_idx = max(warmup_candles, 50)
    for idx in range(start_idx, len(candles) - 1):
        if candles[idx]["close_time"] < analysis_start_ms:
            continue

        snapshot = compute_indicator_snapshot(symbol, candles, idx, context=context)
        row = dict(snapshot)
        row["outcomes"] = {}
        for template_name, template in SETUP_TEMPLATES.items():
            for direction in ("LONG", "SHORT"):
                outcome = simulate_forward_trade(
                    candles=candles,
                    signal_idx=idx,
                    direction=direction,
                    sl_atr_mult=template["sl_atr_mult"],
                    rr_ratio=template["rr_ratio"],
                    max_holding_candles=max_holding_candles,
                    fee_pct=fee_pct,
                    slippage_pct=slippage_pct,
                )
                if outcome is not None:
                    row["outcomes"][_outcome_key(template_name, direction)] = outcome
        if row["outcomes"]:
            rows.append(row)
    return rows


def load_and_build_symbol_rows(
    symbol: str,
    start_ms: int,
    end_ms: int,
    analysis_start_ms: int,
    warmup_candles: int,
    max_holding_candles: int,
    fee_pct: float,
    slippage_pct: float,
) -> tuple[str, list[dict]]:
    """Load historical candles for one symbol and build analyzer rows."""
    candles = fetch_klines_historical_cached(symbol, INTERVAL, start_ms, end_ms, use_cache=True)
    if not candles or len(candles) < warmup_candles + 100:
        log.warning(f"  {symbol}: insufficient candles ({len(candles) if candles else 0}), skipping")
        return symbol, []
    rows = build_symbol_rows(
        symbol=symbol,
        candles=candles,
        analysis_start_ms=analysis_start_ms,
        warmup_candles=warmup_candles,
        max_holding_candles=max_holding_candles,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
    )
    return symbol, rows


def evaluate_subset(
    rows: list[dict],
    template_name: str,
    direction: str,
    condition_names: list[str],
    baseline_stats: SampleStats,
    min_trades: int,
    cooldown: int = EVAL_COOLDOWN_CANDLES,
) -> Optional[dict]:
    """
    Evaluate one condition set with non-overlapping trade selection.

    The subset and baseline both use the same cooldown spacing so the
    comparison is on similarly sampled trades rather than raw row counts.
    """
    outcome_key = _outcome_key(template_name, direction)
    normalized_conditions = normalize_conditions(condition_names)
    matched_rows = []
    for row in rows:
        if outcome_key not in row["outcomes"]:
            continue
        if matches_conditions(row, normalized_conditions):
            matched_rows.append(row)

    matched_rows.sort(key=lambda row: (row.get("close_time", 0), row.get("symbol", "")))
    outcomes = _select_non_overlapping(matched_rows, outcome_key, cooldown)
    stats = summarize_outcomes(outcomes)
    if stats.trades < min_trades:
        return None

    p_value = two_proportion_ztest(stats.wins, stats.trades, baseline_stats.wins, baseline_stats.trades)
    edge_wr = stats.win_rate - baseline_stats.win_rate
    edge_pnl = stats.avg_pnl_pct - baseline_stats.avg_pnl_pct
    return {
        "conditions": normalized_conditions,
        "direction": direction,
        "template": template_name,
        "count": stats.trades,
        "win_rate": round(stats.win_rate, 2),
        "avg_pnl_pct": round(stats.avg_pnl_pct, 4),
        "pnl_sum_pct": round(stats.pnl_sum_pct, 4),
        "baseline_win_rate": round(baseline_stats.win_rate, 2),
        "baseline_avg_pnl_pct": round(baseline_stats.avg_pnl_pct, 4),
        "edge_win_rate": round(edge_wr, 2),
        "edge_avg_pnl_pct": round(edge_pnl, 4),
        "profit_factor": round(stats.profit_factor, 4)
        if stats.profit_factor != float("inf")
        else float("inf"),
        "p_value": round(p_value, 6),
        "wilson_low": round(stats.wilson_low * 100.0, 2),
        "wilson_high": round(stats.wilson_high * 100.0, 2),
    }


def _selection_anchor(row: dict) -> int:
    """Return the time anchor used for cooldown spacing."""
    if "close_time" in row:
        return int(row["close_time"])
    return int(row.get("idx", 0))


def _select_non_overlapping(
    rows: list[dict],
    outcome_key: str,
    cooldown: int = EVAL_COOLDOWN_CANDLES,
) -> list[dict]:
    """Select non-overlapping outcomes using per-symbol cooldown spacing."""
    selected = []
    next_allowed_anchor_by_symbol = {}
    use_time = bool(rows and rows[0].get("close_time") is not None)
    cooldown_step = cooldown * 3_600_000 if use_time else cooldown

    for row in rows:
        symbol = str(row.get("symbol", ""))
        anchor = _selection_anchor(row)
        if anchor < next_allowed_anchor_by_symbol.get(symbol, -1):
            continue
        outcome = row["outcomes"].get(outcome_key)
        if outcome is None:
            continue
        selected.append(outcome)
        next_allowed_anchor_by_symbol[symbol] = anchor + cooldown_step

    return selected


def _compute_baseline(
    rows: list[dict],
    template_name: str,
    direction: str,
    cooldown: int = EVAL_COOLDOWN_CANDLES,
) -> SampleStats:
    """Compute a matched baseline using the same non-overlapping sampling logic."""
    outcome_key = _outcome_key(template_name, direction)
    sorted_rows = sorted(
        [row for row in rows if outcome_key in row["outcomes"]],
        key=lambda row: (row.get("close_time", 0), row.get("symbol", "")),
    )
    outcomes = _select_non_overlapping(sorted_rows, outcome_key, cooldown)
    return summarize_outcomes(outcomes)


def _select_diverse_promising(ordered_conditions: list[str]) -> list[str]:
    """
    From a p-value-sorted list of candidate conditions, keep at most one
    condition per correlation family (CONDITION_FAMILIES).  The first
    representative of each family (lowest p-value) is kept; later members
    of the same family are dropped.  Conditions that belong to no family
    are always kept.
    """
    seen_family_indices: set[int] = set()
    result: list[str] = []
    for cond in ordered_conditions:
        family_idx = next(
            (i for i, family in enumerate(CONDITION_FAMILIES) if cond in family),
            None,
        )
        if family_idx is not None:
            if family_idx in seen_family_indices:
                continue
            seen_family_indices.add(family_idx)
        result.append(cond)
    return result


def analyze_conditions(
    rows: list[dict],
    template_name: str,
    direction: str,
    min_trades: int,
    combo_max_size: int,
    max_combo_tests: int,
    prefilter_p: float,
    cooldown: int = EVAL_COOLDOWN_CANDLES,
) -> tuple[dict, list[dict]]:
    """Analyze individual flat conditions and promising combinations."""
    baseline = _compute_baseline(rows, template_name, direction, cooldown)

    individual_results = []
    for name in ALL_CONDITIONS:
        result = evaluate_subset(rows, template_name, direction, [name], baseline, min_trades, cooldown)
        if result:
            individual_results.append(result)
    individual_results.sort(key=lambda item: (item["p_value"], -item["edge_avg_pnl_pct"]))

    # Apply Benjamini-Hochberg FDR correction across all individual tests so
    # that testing ~65 conditions simultaneously doesn't inflate discoveries.
    if individual_results:
        raw_p = [r["p_value"] for r in individual_results]
        bh_adj = benjamini_hochberg_adjusted(raw_p)
        for i, result in enumerate(individual_results):
            result["bh_adj_p_value"] = round(bh_adj[i], 6)

    # Keep only BH-significant conditions, then deduplicate by family so
    # correlated conditions (rsi_below_28 / rsi_below_30, etc.) don't both
    # enter combos and inflate apparent discoveries.
    bh_promising = [
        result["conditions"][0]
        for result in individual_results
        if result.get("bh_adj_p_value", 1.0) <= prefilter_p
        and result["avg_pnl_pct"] > 0  # only positive-edge conditions enter combos
    ][:MAX_CANDIDATE_CONDITIONS]
    promising = _select_diverse_promising(bh_promising)

    combo_results = []
    tested = 0
    for size in range(2, combo_max_size + 1):
        for combo in combinations(promising, size):
            if tested >= max_combo_tests:
                break
            result = evaluate_subset(rows, template_name, direction, list(combo), baseline, min_trades, cooldown)
            tested += 1
            if result:
                combo_results.append(result)
        if tested >= max_combo_tests:
            break

    combo_results.sort(key=lambda item: (item["p_value"], -item["edge_avg_pnl_pct"]))
    return {
        "baseline": baseline.to_dict(),
        "individual_conditions": individual_results,
        "combinations": combo_results,
    }, combo_results


def _window_subset(rows: list[dict], months: list[str]) -> list[dict]:
    return [row for row in rows if row["month"] in months]


def _setup_name(template_name: str, direction: str, conditions: list[str]) -> str:
    """Build a stable setup identifier from normalized conditions."""
    normalized_conditions = normalize_conditions(conditions)
    return f"{_normalize_template_name(template_name)}_{direction.lower()}_{'_'.join(normalized_conditions)}"


def _runtime_conditions_for_setup(setup: dict) -> list[str]:
    """Export setups exactly as validated, with no hidden condition injection."""
    return normalize_conditions(list(setup.get("conditions", [])))


def _export_runtime_setup(setup: dict) -> dict:
    """Return the production-facing statistical setup payload."""
    exported = {
        "name": setup["name"],
        "template": _normalize_template_name(setup["template"]),
        "direction": setup["direction"],
        "conditions": _runtime_conditions_for_setup(setup),
        "tp_sl": setup["tp_sl"],
        "train_stats": setup["train_stats"],
        "test_stats": setup["test_stats"],
        "by_symbol": setup["by_symbol"],
    }

    scope_context = setup.get("scope_context") or setup  # support both old and new layouts
    scope_type = scope_context.get("scope_type") or setup.get("scope_type")
    scope_symbol = scope_context.get("scope_symbol") or setup.get("scope_symbol")
    scope_regime = scope_context.get("scope_regime") or setup.get("scope_regime")
    if scope_type == "symbol" and scope_symbol:
        exported["scope_key"] = setup.get("scope_key", f"symbol_{scope_symbol}")
        exported["filter"] = {"symbol": scope_symbol}
    elif scope_type == "regime" and scope_regime:
        exported["scope_key"] = setup.get("scope_key", f"regime_{scope_regime}")
        exported["filter"] = {"regime": scope_regime}
    else:
        exported["filter"] = {}

    return exported


def _per_symbol_breakdown(
    rows: list[dict],
    template_name: str,
    direction: str,
    condition_names: list[str],
    cooldown: int = EVAL_COOLDOWN_CANDLES,
) -> dict:
    """Break down a validated setup by symbol using the same sampling rules."""
    breakdown = {}
    for symbol in sorted({row["symbol"] for row in rows}):
        symbol_rows = [row for row in rows if row["symbol"] == symbol]
        if not symbol_rows:
            continue
        result = evaluate_subset(
            symbol_rows,
            template_name,
            direction,
            condition_names,
            _compute_baseline(symbol_rows, template_name, direction, cooldown),
            min_trades=1,
            cooldown=cooldown,
        )
        if result is None:
            continue
        breakdown[symbol] = {
            "trades": result["count"],
            "win_rate": result["win_rate"],
            "avg_pnl_pct": result["avg_pnl_pct"],
            "profit_factor": result["profit_factor"],
        }
    return breakdown


def _normalize_discovery_variants(discovery_variants: Optional[list[str]]) -> list[str]:
    """Return a stable, validated discovery-variant list."""
    variants = discovery_variants or list(DEFAULT_DISCOVERY_VARIANTS)
    normalized = []
    seen = set()
    for variant in variants:
        value = str(variant).strip().lower()
        if value not in VALID_DISCOVERY_VARIANTS or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized or list(DEFAULT_DISCOVERY_VARIANTS)


def tag_rows_with_regime(rows: list[dict], btc_candles: list[dict]) -> None:
    """Tag each row with BTC regime at that row's timestamp (in place).

    Rows whose close_time falls outside the BTC series or during EMA warmup
    are tagged "unknown" and excluded from regime-conditioned views.
    """
    if not btc_candles:
        for row in rows:
            row.setdefault("regime", "unknown")
        return
    lookup = build_regime_lookup(btc_candles)
    for row in rows:
        row["regime"] = lookup.get(int(row.get("close_time", 0)), "unknown")


def build_analysis_views(
    rows: list[dict],
    symbols: list[str],
    discovery_variants: Optional[list[str]] = None,
) -> dict[str, dict]:
    """Build alternative research scopes for edge discovery."""
    variants = _normalize_discovery_variants(discovery_variants)
    symbol_order = symbols or sorted({row["symbol"] for row in rows})
    views = {}

    if "pooled" in variants:
        views["pooled"] = {
            "rows": rows,
            "template_names": sorted(SETUP_TEMPLATES),
            "scope": {"scope_key": "pooled", "scope_type": "pooled", "filter": {}},
        }

    if "symbol_specific" in variants:
        for symbol in symbol_order:
            subset = [row for row in rows if row["symbol"] == symbol]
            if not subset:
                continue
            key = f"symbol_{symbol}"
            views[key] = {
                "rows": subset,
                "template_names": sorted(SETUP_TEMPLATES),
                "scope": {"scope_key": key, "scope_type": "symbol", "scope_symbol": symbol,
                          "filter": {"symbol": symbol}},
            }

    if "regime" in variants:
        for regime_name in VALID_REGIMES:
            subset = [row for row in rows if row.get("regime") == regime_name]
            if not subset:
                continue
            key = f"regime_{regime_name}"
            views[key] = {
                "rows": subset,
                "template_names": sorted(SETUP_TEMPLATES),
                "scope": {"scope_key": key, "scope_type": "regime", "scope_regime": regime_name,
                          "filter": {"regime": regime_name}},
            }

    return views


def validate_candidates(
    rows: list[dict],
    template_name: str,
    direction: str,
    candidate_results: list[dict],
    train_months: int,
    test_months: int,
    step_months: int,
    min_train_trades: int,
    min_test_trades: int,
    validation_p: float,
    cooldown: int = EVAL_COOLDOWN_CANDLES,
    scope_context: Optional[dict] = None,
) -> tuple[list[dict], list[dict]]:
    """Walk-forward validate candidate condition sets."""
    month_starts = [
        datetime.strptime(row["month"], "%Y-%m").replace(tzinfo=timezone.utc)
        for row in rows
    ]
    windows = build_walk_forward_windows(month_starts, train_months, test_months, step_months)
    if not windows:
        return [], []

    validated = []
    rejected = []
    for candidate in candidate_results:
        conditions = normalize_conditions(candidate["conditions"])
        window_results = []
        failed_reason = None
        saw_failure = False
        for window in windows:
            train_rows = _window_subset(rows, window["train_months"])
            test_rows = _window_subset(rows, window["test_months"])
            train_baseline = _compute_baseline(train_rows, template_name, direction, cooldown)
            test_baseline = _compute_baseline(test_rows, template_name, direction, cooldown)

            train_eval = evaluate_subset(
                train_rows, template_name, direction, conditions, train_baseline, min_train_trades, cooldown
            )
            test_eval = evaluate_subset(
                test_rows, template_name, direction, conditions, test_baseline, min_test_trades, cooldown
            )

            if not train_eval or not test_eval:
                if failed_reason is None:
                    failed_reason = "insufficient_sample"
                saw_failure = True
                window_results.append(
                    {
                        "train_months": window["train_months"],
                        "test_months": window["test_months"],
                        "train_stats": train_eval,
                        "test_stats": test_eval,
                        "passed": False,
                        "failure_reason": "insufficient_sample",
                    }
                )
                continue

            train_pass = (
                train_eval["edge_win_rate"] > 5.0
                and train_eval["avg_pnl_pct"] > 0
                and train_eval["p_value"] < validation_p
                and train_eval["profit_factor"] > 1.1
            )
            test_pass = (
                test_eval["edge_win_rate"] > 5.0
                and test_eval["avg_pnl_pct"] > 0
                and test_eval["profit_factor"] > 1.3
            )
            window_results.append(
                {
                    "train_months": window["train_months"],
                    "test_months": window["test_months"],
                    "train_stats": train_eval,
                    "test_stats": test_eval,
                    "passed": bool(train_pass and test_pass),
                }
            )
            if not (train_pass and test_pass):
                if failed_reason is None:
                    failed_reason = "failed_out_of_sample"
                saw_failure = True

        # Require a strict majority of windows to pass (≥ n-1, i.e. at most
        # one failure allowed).  This tolerates a single regime-shift window
        # in a 3-window walk-forward without discarding genuinely consistent edges.
        n_windows = len(window_results)
        n_passed = sum(1 for w in window_results if w.get("passed"))
        majority_pass = n_windows > 0 and n_passed >= n_windows - 1 and n_passed > 0
        if window_results and majority_pass:
            validated_setup = {
                "name": _setup_name(template_name, direction, conditions),
                "template": template_name,
                "direction": direction,
                "conditions": conditions,
                "train_stats": window_results[-1]["train_stats"],
                "test_stats": window_results[-1]["test_stats"],
                "tp_sl": dict(SETUP_TEMPLATES[template_name]),
                "by_symbol": _per_symbol_breakdown(rows, template_name, direction, conditions, cooldown),
                "window_results": window_results,
                "windows_passed": n_passed,
                "windows_total": n_windows,
            }
            if scope_context:
                validated_setup.update(scope_context)
            validated.append(validated_setup)
        else:
            rejection_reason = failed_reason or "no_validation_windows"
            if n_windows > 0 and n_passed < n_windows - 1:
                rejection_reason = f"insufficient_windows_passed_{n_passed}_of_{n_windows}"
            rejected_setup = {
                "name": _setup_name(template_name, direction, conditions),
                "template": template_name,
                "direction": direction,
                "conditions": conditions,
                "reason": rejection_reason,
                "windows_passed": n_passed,
                "windows_total": n_windows,
            }
            if window_results:
                rejected_setup["window_results"] = window_results
            if scope_context:
                rejected_setup.update(scope_context)
            rejected.append(rejected_setup)
    return validated, rejected


def _metric_value(stats: dict, key: str) -> float:
    """Normalize metrics for comparison, including infinite profit factor."""
    value = stats.get(key, 0.0)
    if value == float("inf"):
        return 1_000_000.0
    return float(value)


def _setup_signature(setup: dict) -> tuple:
    """Create a compact signature for detecting effectively identical setups."""
    window_signature = []
    for window in setup.get("window_results", []):
        train = window.get("train_stats")
        test = window.get("test_stats")
        # Skip windows that failed evaluation (insufficient samples → stats=None).
        if not train or not test:
            continue
        window_signature.append(
            (
                tuple(window["train_months"]),
                tuple(window["test_months"]),
                int(train["count"]),
                round(_metric_value(train, "win_rate"), 4),
                round(_metric_value(train, "avg_pnl_pct"), 4),
                round(_metric_value(train, "profit_factor"), 4),
                int(test["count"]),
                round(_metric_value(test, "win_rate"), 4),
                round(_metric_value(test, "avg_pnl_pct"), 4),
                round(_metric_value(test, "profit_factor"), 4),
            )
        )
    by_symbol_signature = tuple(
        (
            symbol,
            int(stats["trades"]),
            round(_metric_value(stats, "win_rate"), 4),
            round(_metric_value(stats, "avg_pnl_pct"), 4),
            round(_metric_value(stats, "profit_factor"), 4),
        )
        for symbol, stats in sorted(setup.get("by_symbol", {}).items())
    )
    return window_signature, by_symbol_signature


def _average_window_metric(setup: dict, split: str, metric: str) -> float:
    """Average one metric across validation windows, falling back to summary stats."""
    values = [
        _metric_value(window[f"{split}_stats"], metric)
        for window in setup.get("window_results", [])
        if window.get(f"{split}_stats") is not None
    ]
    if values:
        return sum(values) / len(values)
    return _metric_value(setup.get(f"{split}_stats") or {}, metric)


def _is_superset(candidate: dict, simpler_setup: dict) -> bool:
    """True when candidate is a strict condition superset of a simpler setup."""
    candidate_conditions = set(candidate["conditions"])
    simpler_conditions = set(simpler_setup["conditions"])
    return len(candidate_conditions) > len(simpler_conditions) and simpler_conditions.issubset(candidate_conditions)


def _has_material_increment(candidate: dict, simpler_setup: dict) -> bool:
    """Require a stricter setup to add meaningful train/test improvement."""
    deltas = {
        "train_avg_pnl_pct": _average_window_metric(candidate, "train", "avg_pnl_pct")
        - _average_window_metric(simpler_setup, "train", "avg_pnl_pct"),
        "test_avg_pnl_pct": _average_window_metric(candidate, "test", "avg_pnl_pct")
        - _average_window_metric(simpler_setup, "test", "avg_pnl_pct"),
        "train_profit_factor": _average_window_metric(candidate, "train", "profit_factor")
        - _average_window_metric(simpler_setup, "train", "profit_factor"),
        "test_profit_factor": _average_window_metric(candidate, "test", "profit_factor")
        - _average_window_metric(simpler_setup, "test", "profit_factor"),
        "test_edge_win_rate": _average_window_metric(candidate, "test", "edge_win_rate")
        - _average_window_metric(simpler_setup, "test", "edge_win_rate"),
    }
    return any(deltas[key] >= threshold for key, threshold in DOMINANCE_THRESHOLDS.items())


def dedupe_validated_setups(validated_setups: list[dict]) -> tuple[list[dict], list[dict]]:
    """Remove redundant validated setups while preserving meaningfully distinct ones."""
    normalized_setups = []
    for setup in validated_setups:
        normalized = dict(setup)
        normalized["conditions"] = normalize_conditions(setup["conditions"])
        normalized["template"] = _normalize_template_name(setup["template"])
        normalized["name"] = _setup_name(setup["template"], setup["direction"], normalized["conditions"])
        normalized_setups.append(normalized)

    normalized_setups.sort(
        key=lambda setup: (
            setup["template"],
            setup["direction"],
            len(setup["conditions"]),
            -_metric_value(setup["test_stats"], "profit_factor"),
            -_metric_value(setup["test_stats"], "avg_pnl_pct"),
            setup["name"],
        )
    )

    kept = []
    removed = []
    for candidate in normalized_setups:
        removal = None
        for simpler_setup in kept:
            if candidate["template"] != simpler_setup["template"] or candidate["direction"] != simpler_setup["direction"]:
                continue
            if set(candidate["conditions"]) == set(simpler_setup["conditions"]):
                removal = {
                    "name": candidate["name"],
                    "template": candidate["template"],
                    "direction": candidate["direction"],
                    "conditions": candidate["conditions"],
                    "reason": "duplicate_conditions",
                    "kept_setup": simpler_setup["name"],
                }
                break
            if not _is_superset(candidate, simpler_setup):
                continue
            if _setup_signature(candidate) == _setup_signature(simpler_setup):
                removal = {
                    "name": candidate["name"],
                    "template": candidate["template"],
                    "direction": candidate["direction"],
                    "conditions": candidate["conditions"],
                    "reason": "redundant_superset",
                    "kept_setup": simpler_setup["name"],
                }
                break
            if not _has_material_increment(candidate, simpler_setup):
                removal = {
                    "name": candidate["name"],
                    "template": candidate["template"],
                    "direction": candidate["direction"],
                    "conditions": candidate["conditions"],
                    "reason": "dominated_superset",
                    "kept_setup": simpler_setup["name"],
                }
                break
        if removal:
            removed.append(removal)
            continue
        kept.append(candidate)

    return kept, removed


def build_validated_setups_export(report: dict) -> dict:
    """Create the lightweight production-facing validated setup export.

    Includes pooled setups plus any setups from discovery variants (symbol_specific,
    regime). Variant setups carry their scope (scope_symbol or scope_regime) so the
    scanner can gate them at runtime.
    """
    validated_long = list(report["validated_setups"]["long"])
    validated_short = list(report["validated_setups"]["short"])
    for variant_report in report.get("discovery_variants", {}).values():
        validated_long.extend(variant_report.get("validated_setups", {}).get("long", []))
        validated_short.extend(variant_report.get("validated_setups", {}).get("short", []))
    dedupe_removed = report.get("dedupe_removed_setups", {"long": [], "short": []})

    return {
        "schema_version": 1,
        "metadata": {
            "generated_at": report["metadata"]["generated_at"],
            "data_range": report["metadata"]["data_range"],
            "symbols": report["metadata"]["symbols"],
            "train_months": report["metadata"]["train_months"],
            "test_months": report["metadata"]["test_months"],
            "step_months": report["metadata"]["step_months"],
            "eval_cooldown_candles": report["metadata"]["eval_cooldown_candles"],
            "fee_pct": report["metadata"]["fee_pct"],
            "slippage_pct": report["metadata"]["slippage_pct"],
            "dedupe_policy": {
                "drop_strict_superset_if_signature_matches": True,
                "dominance_thresholds": dict(DOMINANCE_THRESHOLDS),
                "removed_counts": {
                    "long": len(dedupe_removed.get("long", [])),
                    "short": len(dedupe_removed.get("short", [])),
                },
            },
        },
        "validated_setups": {
            "long": [_export_runtime_setup(setup) for setup in validated_long],
            "short": [_export_runtime_setup(setup) for setup in validated_short],
        },
    }

def analyze_rows_scope(
    rows: list[dict],
    template_names: list[str],
    combo_max_size: int,
    max_combo_tests: int,
    min_train_trades: int,
    min_test_trades: int,
    prefilter_p: float,
    validation_p: float,
    train_months: int,
    test_months: int,
    step_months: int,
    scope_context: Optional[dict] = None,
) -> dict:
    """Run the full setup-discovery pipeline on one research scope."""
    scope_context = dict(scope_context or {})
    report = {
        "scope": scope_context,
        "summary": {
            "rows_analyzed": len(rows),
            "symbols_in_scope": sorted({row["symbol"] for row in rows}),
            "templates_in_scope": list(template_names),
        },
        "analysis": {},
        "validated_setups": {"long": [], "short": []},
        "raw_validated_setups": {"long": [], "short": []},
        "dedupe_removed_setups": {"long": [], "short": []},
        "rejected_setups": [],
    }

    for template_name in template_names:
        for direction in ("LONG", "SHORT"):
            analysis_key = f"{template_name}_{direction.lower()}"
            analysis_result, combo_results = analyze_conditions(
                rows=rows,
                template_name=template_name,
                direction=direction,
                min_trades=min_train_trades,
                combo_max_size=combo_max_size,
                max_combo_tests=max_combo_tests,
                prefilter_p=prefilter_p,
            )
            report["analysis"][analysis_key] = analysis_result
            promising_individuals = [
                result
                for result in analysis_result["individual_conditions"]
                if result["p_value"] < prefilter_p and result["avg_pnl_pct"] > 0
            ]
            validated, rejected = validate_candidates(
                rows=rows,
                template_name=template_name,
                direction=direction,
                candidate_results=promising_individuals + combo_results,
                train_months=train_months,
                test_months=test_months,
                step_months=step_months,
                min_train_trades=min_train_trades,
                min_test_trades=min_test_trades,
                validation_p=validation_p,
                cooldown=EVAL_COOLDOWN_CANDLES,
                scope_context=scope_context,
            )
            bucket = "long" if direction == "LONG" else "short"
            report["raw_validated_setups"][bucket].extend(validated)
            report["rejected_setups"].extend(rejected)

    for bucket in ("long", "short"):
        deduped, removed = dedupe_validated_setups(report["raw_validated_setups"][bucket])
        report["validated_setups"][bucket] = deduped
        report["dedupe_removed_setups"][bucket] = removed

    report["summary"].update(
        {
            "validated_long_raw": len(report["raw_validated_setups"]["long"]),
            "validated_short_raw": len(report["raw_validated_setups"]["short"]),
            "validated_long": len(report["validated_setups"]["long"]),
            "validated_short": len(report["validated_setups"]["short"]),
            "rejected": len(report["rejected_setups"]),
        }
    )
    return report


def analyze_symbol_universe(
    symbols: list[str],
    months: int,
    warmup_candles: int,
    max_holding_candles: int,
    combo_max_size: int,
    max_combo_tests: int,
    min_train_trades: int,
    min_test_trades: int,
    fee_pct: float,
    slippage_pct: float,
    train_months: int,
    test_months: int,
    step_months: int,
    prefilter_p: float,
    validation_p: float,
    end_dt: Optional[datetime] = None,
    workers: int = 1,
    discovery_variants: Optional[list[str]] = None,
) -> dict:
    """Run the full analysis workflow and return the JSON-serializable report."""
    normalized_variants = _normalize_discovery_variants(discovery_variants)
    end_dt = end_dt.astimezone(timezone.utc) if end_dt is not None else datetime.now(timezone.utc)
    warmup_days = 170
    analysis_start_dt = end_dt - timedelta(days=30 * months)
    fetch_start_dt = analysis_start_dt - timedelta(days=warmup_days)
    start_ms = int(fetch_start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    analysis_start_ms = int(analysis_start_dt.timestamp() * 1000)

    worker_count = max(1, workers)

    # Pre-populate cross-symbol context for the new condition families.
    _populate_btc_and_funding_context(symbols, start_ms, end_ms)

    all_rows, _, candles_by_symbol = load_symbol_universe_rows(
        symbols=symbols,
        start_ms=start_ms,
        end_ms=end_ms,
        analysis_start_ms=analysis_start_ms,
        warmup_candles=warmup_candles,
        max_holding_candles=max_holding_candles,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        workers=worker_count,
    )

    # Tag every row with the BTC regime at its timestamp so regime-conditioned
    # discovery variants can filter rows later. Tagging is always safe; the
    # tag is only consumed when the "regime" variant is requested.
    btc_candles = candles_by_symbol.get(REGIME_BTC_SYMBOL, []) if candles_by_symbol else []
    tag_rows_with_regime(all_rows, btc_candles)
    if btc_candles:
        regime_counts: dict[str, int] = {}
        for row in all_rows:
            regime_counts[row["regime"]] = regime_counts.get(row["regime"], 0) + 1
        log.info("Regime row counts: %s", regime_counts)

    report = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_range": f"{analysis_start_dt.strftime('%Y-%m')} to {end_dt.strftime('%Y-%m')}",
            "symbols": symbols,
            "rows_analyzed": len(all_rows),
            "conditions_tested": len(ALL_CONDITIONS),
            "warmup_candles": warmup_candles,
            "max_holding_candles": max_holding_candles,
            "eval_cooldown_candles": EVAL_COOLDOWN_CANDLES,
            "fee_pct": fee_pct,
            "slippage_pct": slippage_pct,
            "combo_max_size": combo_max_size,
            "max_combo_tests": max_combo_tests,
            "train_months": train_months,
            "test_months": test_months,
            "step_months": step_months,
            "workers": worker_count,
            "discovery_variants": normalized_variants,
        },
        "analysis": {},
        "validated_setups": {"long": [], "short": []},
        "raw_validated_setups": {"long": [], "short": []},
        "dedupe_removed_setups": {"long": [], "short": []},
        "rejected_setups": [],
        "summary": {},
    }

    pooled_report = analyze_rows_scope(
        rows=all_rows,
        template_names=sorted(SETUP_TEMPLATES),
        combo_max_size=combo_max_size,
        max_combo_tests=max_combo_tests,
        min_train_trades=min_train_trades,
        min_test_trades=min_test_trades,
        prefilter_p=prefilter_p,
        validation_p=validation_p,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
        scope_context={"scope_key": "pooled", "scope_type": "pooled"},
    )
    report["analysis"] = pooled_report["analysis"]
    report["validated_setups"] = pooled_report["validated_setups"]
    report["raw_validated_setups"] = pooled_report["raw_validated_setups"]
    report["dedupe_removed_setups"] = pooled_report["dedupe_removed_setups"]
    report["rejected_setups"] = pooled_report["rejected_setups"]
    report["summary"] = pooled_report["summary"]

    variant_views = build_analysis_views(all_rows, symbols, normalized_variants)
    extra_variant_reports = {}
    for view_key, view in variant_views.items():
        if view_key == "pooled":
            continue
        extra_variant_reports[view_key] = analyze_rows_scope(
            rows=view["rows"],
            template_names=view["template_names"],
            combo_max_size=combo_max_size,
            max_combo_tests=max_combo_tests,
            min_train_trades=min_train_trades,
            min_test_trades=min_test_trades,
            prefilter_p=prefilter_p,
            validation_p=validation_p,
            train_months=train_months,
            test_months=test_months,
            step_months=step_months,
            scope_context=view["scope"],
        )
    if extra_variant_reports:
        report["discovery_variants"] = extra_variant_reports

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Statistical setup analyzer for the trading bot")
    parser.add_argument("--months", type=int, default=12, help="Months of data to analyze")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="Symbols to analyze")
    parser.add_argument("--warmup-candles", type=int, default=DEFAULT_WARMUP_CANDLES, help="Warmup candles before analysis")
    parser.add_argument("--max-holding-candles", type=int, default=DEFAULT_MAX_HOLDING_CANDLES, help="Max holding period in candles")
    parser.add_argument("--combo-max-size", type=int, default=3, help="Largest condition combo size to test")
    parser.add_argument("--max-combo-tests", type=int, default=MAX_COMBO_TESTS_DEFAULT, help="Maximum combo tests per direction/template")
    parser.add_argument("--min-train-trades", type=int, default=DEFAULT_MIN_TRAIN_TRADES, help="Minimum train-sample trades")
    parser.add_argument("--min-test-trades", type=int, default=DEFAULT_MIN_TEST_TRADES, help="Minimum test-sample trades")
    parser.add_argument("--train-months", type=int, default=6, help="Walk-forward training months")
    parser.add_argument("--test-months", type=int, default=3, help="Walk-forward test months")
    parser.add_argument("--step-months", type=int, default=3, help="Walk-forward shift size")
    parser.add_argument("--prefilter-p", type=float, default=DEFAULT_PREFILTER_P, help="P-value threshold for combo prefiltering")
    parser.add_argument("--validation-p", type=float, default=DEFAULT_VALIDATION_P, help="P-value threshold for validation")
    parser.add_argument("--fee-pct", type=float, default=DEFAULT_FEE_PCT, help="Round-trip fee percent")
    parser.add_argument("--slippage-pct", type=float, default=DEFAULT_SLIPPAGE_PCT, help="Adverse slippage percent per side")
    parser.add_argument(
        "--discovery-variants",
        nargs="+",
        choices=VALID_DISCOVERY_VARIANTS,
        default=list(DEFAULT_DISCOVERY_VARIANTS),
        help="Additional research views to run: pooled, symbol_specific, regime",
    )
    parser.add_argument("--end-date", type=str, default=None, help="Optional UTC end date (YYYY-MM-DD) for reproducible cached analysis")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1), help="Parallel workers for per-symbol row building")
    parser.add_argument("--interval", type=str, default="1h", choices=["1h", "4h", "1d"], help="Candle interval")
    parser.add_argument("--output", type=str, default=DEFAULT_REPORT_OUTPUT, help="Full analysis report JSON path")
    parser.add_argument(
        "--validated-output",
        type=str,
        default=DEFAULT_VALIDATED_OUTPUT,
        help="Deduped validated setups JSON export path",
    )
    args = parser.parse_args()

    end_dt = None
    if args.end_date:
        end_dt = datetime.strptime(args.end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    global INTERVAL
    INTERVAL = args.interval

    report = analyze_symbol_universe(
        symbols=args.symbols,
        months=args.months,
        warmup_candles=args.warmup_candles,
        max_holding_candles=args.max_holding_candles,
        combo_max_size=args.combo_max_size,
        max_combo_tests=args.max_combo_tests,
        min_train_trades=args.min_train_trades,
        min_test_trades=args.min_test_trades,
        fee_pct=args.fee_pct,
        slippage_pct=args.slippage_pct,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        prefilter_p=args.prefilter_p,
        validation_p=args.validation_p,
        end_dt=end_dt,
        workers=args.workers,
        discovery_variants=args.discovery_variants,
    )

    validated_export = build_validated_setups_export(report)

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    with open(args.validated_output, "w", encoding="utf-8") as handle:
        json.dump(validated_export, handle, indent=2)

    log.info(
        "Saved analysis to %s",
        args.output,
    )
    log.info(
        "Saved validated setups to %s (%d long validated, %d short validated)",
        args.validated_output,
        len(validated_export["validated_setups"]["long"]),
        len(validated_export["validated_setups"]["short"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
