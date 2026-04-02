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
        "adx_above_25": lambda ind: ind["adx"] > 25,
        "adx_above_30": lambda ind: ind["adx"] > 30,
        "adx_below_25": lambda ind: ind["adx"] < 25,
        "adx_below_20": lambda ind: ind["adx"] < 20,
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
        "london_session": lambda ind: 8 <= ind["hour_utc"] <= 16,
        "us_session": lambda ind: 13 <= ind["hour_utc"] <= 21,
        "not_asia": lambda ind: not (0 <= ind["hour_utc"] < 8),
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
