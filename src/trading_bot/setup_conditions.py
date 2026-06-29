"""Shared setup condition definitions for statistical setup research and matching."""

from __future__ import annotations

from typing import Callable


def build_conditions() -> dict[str, Callable[[dict], bool]]:
    """Return the shared factual condition map used by analyzer and scanner."""
    return {
        "rsi_below_28": lambda ind: ind["rsi"] < 28,
        "rsi_below_30": lambda ind: ind["rsi"] < 30,
        "rsi_below_32": lambda ind: ind["rsi"] < 32,
        "rsi_below_40": lambda ind: ind["rsi"] < 40,
        "rsi_35_to_50": lambda ind: 35 <= ind["rsi"] <= 50,
        "rsi_40_to_60": lambda ind: 40 <= ind["rsi"] <= 60,
        "rsi_above_60": lambda ind: ind["rsi"] > 60,
        "rsi_above_70": lambda ind: ind["rsi"] > 70,
        "ema_9_above_21_above_50": lambda ind: ind["e9"] > ind["e21"] > ind["e50"],
        "ema_9_below_21_below_50": lambda ind: ind["e9"] < ind["e21"] < ind["e50"],
        "ema9_cross_above_21": lambda ind: ind["e9"] > ind["e21"] and ind["e9_prev"] <= ind["e21_prev"],
        "ema9_cross_below_21": lambda ind: ind["e9"] < ind["e21"] and ind["e9_prev"] >= ind["e21_prev"],
        "ema50_cross_above_200": lambda ind: ind["e50"] > ind["e200"] and ind["e50_prev"] <= ind["e200_prev"],
        "ema50_cross_below_200": lambda ind: ind["e50"] < ind["e200"] and ind["e50_prev"] >= ind["e200_prev"],
        "above_ema200": lambda ind: ind["above_ema200"],
        "below_ema200": lambda ind: ind["below_ema200"],
        "price_near_ema21": lambda ind: abs(ind["close"] - ind["e21"]) / max(ind["close"], 1e-9) < 0.005,
        "macd_line_above_signal": lambda ind: ind["macd_line"] > ind["macd_signal"],
        "macd_line_below_signal": lambda ind: ind["macd_line"] < ind["macd_signal"],
        "macd_hist_positive": lambda ind: ind["macd_hist"] > 0,
        "macd_hist_negative": lambda ind: ind["macd_hist"] < 0,
        "macd_hist_rising": lambda ind: ind["macd_hist"] > ind["macd_hist_prev"],
        "macd_hist_falling": lambda ind: ind["macd_hist"] < ind["macd_hist_prev"],
        "macd_cross_up": lambda ind: ind["macd_hist"] > 0 and ind["macd_hist_prev"] <= 0,
        "macd_cross_down": lambda ind: ind["macd_hist"] < 0 and ind["macd_hist_prev"] >= 0,
        "rsi_bullish_divergence": lambda ind: ind["rsi_bullish_divergence"],
        "rsi_bearish_divergence": lambda ind: ind["rsi_bearish_divergence"],
        "macd_bullish_divergence": lambda ind: ind["macd_bullish_divergence"],
        "macd_bearish_divergence": lambda ind: ind["macd_bearish_divergence"],
        "adx_above_25": lambda ind: ind["adx"] > 25,
        "adx_above_30": lambda ind: ind["adx"] > 30,
        "adx_below_25": lambda ind: ind["adx"] < 25,
        "adx_below_20": lambda ind: ind["adx"] < 20,
        "adx_rising": lambda ind: ind["adx_rising"],
        "adx_falling": lambda ind: ind["adx_falling"],
        "vol_above_1_2": lambda ind: ind["vol_ratio"] > 1.2,
        "vol_above_1_5": lambda ind: ind["vol_ratio"] > 1.5,
        "vol_above_2_0": lambda ind: ind["vol_ratio"] > 2.0,
        "bb_squeeze": lambda ind: ind["bb_squeeze"],
        "price_above_upper_bb": lambda ind: ind["close"] > ind["bb_upper"],
        "price_below_lower_bb": lambda ind: ind["close"] < ind["bb_lower"],
        "price_near_lower_bb": lambda ind: 0 < (ind["close"] - ind["bb_lower"]) / max(ind["close"], 1e-9) < 0.01,
        "price_near_upper_bb": lambda ind: 0 < (ind["bb_upper"] - ind["close"]) / max(ind["close"], 1e-9) < 0.01,
        "higher_highs": lambda ind: ind["higher_highs"],
        "lower_lows": lambda ind: ind["lower_lows"],
        "bullish_engulfing": lambda ind: ind["bullish_engulfing"],
        "bearish_engulfing": lambda ind: ind["bearish_engulfing"],
        "inside_bar": lambda ind: ind["inside_bar"],
        "outside_bar": lambda ind: ind["outside_bar"],
        "pin_bar_bull": lambda ind: ind["pin_bar_bull"],
        "pin_bar_bear": lambda ind: ind["pin_bar_bear"],
        "three_green_candles": lambda ind: ind["three_green_candles"],
        "three_red_candles": lambda ind: ind["three_red_candles"],
        "ema21_rising": lambda ind: ind["ema21_rising"],
        "ema21_falling": lambda ind: ind["ema21_falling"],
        "ema_fan_wide": lambda ind: ind["ema_fan_wide"],
        "atr_percentile_high": lambda ind: ind["atr_percentile_high"],
        "atr_expansion": lambda ind: ind["atr_expansion"],
        "candle_range_above_atr": lambda ind: ind["candle_range_above_atr"],
        "two_expansion_green_candles": lambda ind: ind["two_expansion_green_candles"],
        "two_expansion_red_candles": lambda ind: ind["two_expansion_red_candles"],
        "strong_ema21_slope_up": lambda ind: ind["strong_ema21_slope_up"],
        "strong_ema21_slope_down": lambda ind: ind["strong_ema21_slope_down"],
        "near_20bar_high": lambda ind: ind["near_20bar_high"],
        "near_20bar_low": lambda ind: ind["near_20bar_low"],
        "breaks_20bar_high": lambda ind: ind["breaks_20bar_high"],
        "breaks_20bar_low": lambda ind: ind["breaks_20bar_low"],
        "htf_4h_bull_trend": lambda ind: ind["htf_4h_bull_trend"],
        "htf_4h_bear_trend": lambda ind: ind["htf_4h_bear_trend"],
        "london_session": lambda ind: 8 <= ind["hour_utc"] <= 16,
        "us_session": lambda ind: 13 <= ind["hour_utc"] <= 21,
        "not_asia": lambda ind: not (0 <= ind["hour_utc"] < 8),
        # Taker buy aggressiveness (intra-candle buying vs selling pressure)
        "taker_buy_ratio_above_0_55": lambda ind: ind.get("taker_buy_ratio", 0.5) > 0.55,
        "taker_buy_ratio_above_0_60": lambda ind: ind.get("taker_buy_ratio", 0.5) > 0.60,
        "taker_buy_ratio_below_0_45": lambda ind: ind.get("taker_buy_ratio", 0.5) < 0.45,
        "taker_buy_ratio_below_0_40": lambda ind: ind.get("taker_buy_ratio", 0.5) < 0.40,
        # BTC contagion (cross-symbol macro pressure)
        "btc_dump_4h": lambda ind: ind.get("btc_pct_4h", 0.0) < -2.0,
        "btc_pump_4h": lambda ind: ind.get("btc_pct_4h", 0.0) > 2.0,
        "btc_flat_4h": lambda ind: abs(ind.get("btc_pct_4h", 0.0)) < 0.5,
        # Funding rate extremes (crowded-position contrarian signal)
        "funding_above_0_01": lambda ind: ind.get("funding_rate", 0.0) > 0.0001,
        "funding_above_0_05": lambda ind: ind.get("funding_rate", 0.0) > 0.0005,
        "funding_below_neg_0_01": lambda ind: ind.get("funding_rate", 0.0) < -0.0001,
        "funding_below_neg_0_05": lambda ind: ind.get("funding_rate", 0.0) < -0.0005,
    }


ALL_CONDITIONS = build_conditions()


def normalize_conditions(condition_names: list[str]) -> list[str]:
    """Return a stable, duplicate-free condition list."""
    return sorted(dict.fromkeys(condition_names))


def matches_conditions(snapshot: dict, condition_names: list[str]) -> bool:
    """Evaluate one snapshot against a condition set."""
    normalized = normalize_conditions(condition_names)
    unknown = [name for name in normalized if name not in ALL_CONDITIONS]
    if unknown:
        raise ValueError(f"Unknown setup condition(s): {', '.join(unknown)}")
    return all(ALL_CONDITIONS[name](snapshot) for name in normalized)
