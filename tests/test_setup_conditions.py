import os
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_bot.setup_conditions import matches_conditions


def make_snapshot(**overrides):
    base = {
        "rsi": 50.0,
        "e9": 101.0,
        "e21": 100.0,
        "e50": 99.0,
        "e200": 98.0,
        "e9_prev": 99.0,
        "e21_prev": 100.0,
        "e50_prev": 97.0,
        "e200_prev": 98.0,
        "above_ema200": True,
        "below_ema200": False,
        "close": 100.0,
        "macd_line": 1.0,
        "macd_signal": 0.5,
        "macd_hist": 0.5,
        "macd_hist_prev": 0.1,
        "adx": 30.0,
        "vol_ratio": 1.0,
        "bb_squeeze": False,
        "bb_upper": 105.0,
        "bb_lower": 95.0,
        "higher_highs": False,
        "lower_lows": False,
        "bullish_engulfing": False,
        "bearish_engulfing": False,
        "inside_bar": False,
        "outside_bar": False,
        "pin_bar_bull": False,
        "pin_bar_bear": False,
        "three_green_candles": False,
        "three_red_candles": False,
        "ema21_rising": True,
        "ema21_falling": False,
        "ema_fan_wide": False,
        "strong_ema21_slope_up": False,
        "strong_ema21_slope_down": False,
        "adx_rising": True,
        "adx_falling": False,
        "atr_percentile_high": False,
        "atr_expansion": False,
        "candle_range_above_atr": False,
        "two_expansion_green_candles": False,
        "two_expansion_red_candles": False,
        "near_20bar_high": False,
        "near_20bar_low": False,
        "breaks_20bar_high": False,
        "breaks_20bar_low": False,
        "rsi_bullish_divergence": False,
        "rsi_bearish_divergence": False,
        "macd_bullish_divergence": False,
        "macd_bearish_divergence": False,
        "htf_4h_bull_trend": False,
        "htf_4h_bear_trend": False,
        "hour_utc": 14,
    }
    base.update(overrides)
    return base


def test_matches_conditions_accepts_fast_bullish_cross():
    snapshot = make_snapshot(
        e9=101.0,
        e21=100.0,
        e9_prev=99.0,
        e21_prev=100.0,
    )

    assert matches_conditions(snapshot, ["ema9_cross_above_21"])


def test_matches_conditions_accepts_fast_bearish_cross():
    snapshot = make_snapshot(
        e9=99.0,
        e21=100.0,
        e9_prev=101.0,
        e21_prev=100.0,
    )

    assert matches_conditions(snapshot, ["ema9_cross_below_21"])


def test_matches_conditions_accepts_slow_bullish_cross():
    snapshot = make_snapshot(
        e50=100.0,
        e200=99.0,
        e50_prev=98.0,
        e200_prev=99.0,
    )

    assert matches_conditions(snapshot, ["ema50_cross_above_200"])


def test_matches_conditions_accepts_slow_bearish_cross():
    snapshot = make_snapshot(
        e50=98.0,
        e200=99.0,
        e50_prev=100.0,
        e200_prev=99.0,
    )

    assert matches_conditions(snapshot, ["ema50_cross_below_200"])


def test_matches_conditions_rejects_unknown_condition():
    with pytest.raises(ValueError, match="Unknown setup condition"):
        matches_conditions(make_snapshot(), ["totally_unknown_condition"])


def test_matches_conditions_accepts_price_action_conditions():
    snapshot = make_snapshot(
        bullish_engulfing=True,
        inside_bar=True,
        three_green_candles=True,
    )

    assert matches_conditions(snapshot, ["bullish_engulfing", "inside_bar", "three_green_candles"])


def test_matches_conditions_accepts_trend_and_volatility_conditions():
    snapshot = make_snapshot(
        ema21_rising=True,
        ema_fan_wide=True,
        adx_rising=True,
        atr_expansion=True,
        atr_percentile_high=True,
        candle_range_above_atr=True,
        near_20bar_high=True,
    )

    assert matches_conditions(
        snapshot,
        [
            "ema21_rising",
            "ema_fan_wide",
            "adx_rising",
            "atr_expansion",
            "atr_percentile_high",
            "candle_range_above_atr",
            "near_20bar_high",
        ],
    )


def test_matches_conditions_accepts_new_divergence_and_breakout_conditions():
    snapshot = make_snapshot(
        rsi_bullish_divergence=True,
        macd_bullish_divergence=True,
        breaks_20bar_high=True,
        htf_4h_bull_trend=True,
    )

    assert matches_conditions(
        snapshot,
        [
            "rsi_bullish_divergence",
            "macd_bullish_divergence",
            "breaks_20bar_high",
            "htf_4h_bull_trend",
        ],
    )


def test_matches_conditions_accepts_new_slope_and_expansion_conditions():
    snapshot = make_snapshot(
        strong_ema21_slope_up=True,
        two_expansion_green_candles=True,
    )

    assert matches_conditions(snapshot, ["strong_ema21_slope_up", "two_expansion_green_candles"])
