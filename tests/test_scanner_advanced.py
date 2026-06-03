"""
Advanced tests for scanner.py — testing gaps in filter logic, regime detection, and strategy-specific scoring.

These tests cover:
- All 4 signal filters with exact boundary conditions
- detect_regime() all branches
- is_whipsaw() state machine
- suggest_tp_sl() with all strategy multipliers
- Edge cases and unusual configurations
"""

import pytest
import sys
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from trading_bot import scanner as market_scanner

# Alias so legacy @patch("market_scanner.X") decorators still resolve.
sys.modules.setdefault('market_scanner', market_scanner)

# Import key functions to test
generate_signal = market_scanner.generate_signal
detect_regime = market_scanner.detect_regime
is_whipsaw = market_scanner.is_whipsaw
suggest_tp_sl = market_scanner.suggest_tp_sl
score_technical = market_scanner.score_technical


# ─────────────────────────────────────────────────────────────
# Helper fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def trending_up_1000():
    """1000 candles in strong uptrend — always produces a signal."""
    candles = []
    price = 100.0
    for i in range(1000):
        price += 0.15
        candles.append([price - 0.05, price + 0.10, price - 0.10, price, 1000.0 + (i % 50) * 20])
    return candles


# ─────────────────────────────────────────────────────────────
# A. detect_regime() tests — all 4 branches
# ─────────────────────────────────────────────────────────────

class TestDetectRegime:
    """Test all 4 regime branches."""

    def test_regime_breakout_when_squeeze_and_high_volume(self):
        """Bollinger squeeze + high volume → 'breakout' (overrides ADX)."""
        regime = detect_regime(adx_val=10, bb_squeeze=True, vol_r=2.0, closes=[100] * 50)
        assert regime == "breakout"

    def test_regime_trending_when_adx_above_25(self):
        """ADX >= 25 → 'trending'."""
        regime = detect_regime(adx_val=30, bb_squeeze=False, vol_r=1.0, closes=[100] * 50)
        assert regime == "trending"

    def test_regime_weak_trend_when_adx_20_to_25(self):
        """ADX in [20, 25) → 'weak_trend'."""
        regime = detect_regime(adx_val=22, bb_squeeze=False, vol_r=1.0, closes=[100] * 50)
        assert regime == "weak_trend"

    def test_regime_weak_trend_when_adx_below_20(self):
        """ADX < 20 → 'weak_trend' (not 'ranging', that branch disabled)."""
        regime = detect_regime(adx_val=10, bb_squeeze=False, vol_r=1.0, closes=[100] * 50)
        assert regime == "weak_trend"

    def test_regime_breakout_wins_over_trending_adx(self):
        """When both squeeze and ADX 25+, squeeze wins → 'breakout'."""
        regime = detect_regime(adx_val=30, bb_squeeze=True, vol_r=2.0, closes=[100] * 50)
        assert regime == "breakout"


# ─────────────────────────────────────────────────────────────
# B. Asia Session Filter (Filter 4) — boundary conditions
# ─────────────────────────────────────────────────────────────

class TestAsiaSessionFilter:
    """Test Filter 4: Asia session (0-8 UTC) rejection with exact boundary conditions."""

    @patch('market_scanner.score_technical')
    def test_asia_session_hour_0_rejected(self, mock_score, trending_up_1000):
        """UTC hour 0 (00:30) → filtered."""
        mock_score.return_value = {
            "direction": "LONG",
            "long_score": 5.0,
            "short_score": 0.0,
            "details": {"rsi": {"1h": 50}, "adx": 30}
        }

        current_time = datetime(2025, 1, 1, 0, 30, tzinfo=timezone.utc)
        signal = generate_signal("BTC", trending_up_1000, current_time=current_time)
        assert signal is None, "Asia session hour 0 should be filtered"

    @patch('market_scanner.score_technical')
    def test_asia_session_hour_7_rejected(self, mock_score, trending_up_1000):
        """UTC hour 7 (07:59) → filtered."""
        mock_score.return_value = {
            "direction": "LONG",
            "long_score": 5.0,
            "short_score": 0.0,
            "details": {"rsi": {"1h": 50}, "adx": 30}
        }

        current_time = datetime(2025, 1, 1, 7, 59, tzinfo=timezone.utc)
        signal = generate_signal("BTC", trending_up_1000, current_time=current_time)
        assert signal is None, "Asia session hour 7 should be filtered"

    @patch('market_scanner.score_technical')
    def test_asia_session_hour_8_allowed(self, mock_score, trending_up_1000):
        """UTC hour 8 (08:00) → NOT filtered (boundary is exclusive: [0, 8))."""
        mock_score.return_value = {
            "direction": "LONG",
            "long_score": 5.0,
            "short_score": 0.0,
            "details": {"rsi": {"1h": 50}, "adx": 30}
        }

        current_time = datetime(2025, 1, 1, 8, 0, tzinfo=timezone.utc)
        signal = generate_signal("BTC", trending_up_1000, current_time=current_time)
        assert signal is not None, "Hour 8 UTC should NOT be filtered"

    @patch('market_scanner.score_technical')
    def test_non_asia_session_hour_12_allowed(self, mock_score, trending_up_1000):
        """UTC hour 12 → NOT filtered."""
        mock_score.return_value = {
            "direction": "LONG",
            "long_score": 5.0,
            "short_score": 0.0,
            "details": {"rsi": {"1h": 50}, "adx": 30}
        }

        current_time = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        signal = generate_signal("BTC", trending_up_1000, current_time=current_time)
        assert signal is not None, "Hour 12 UTC should NOT be filtered"


# ─────────────────────────────────────────────────────────────
# C. is_whipsaw() state machine
# ─────────────────────────────────────────────────────────────

class TestWhipsawDetection:
    """Test is_whipsaw() with various state transitions."""

    def test_whipsaw_fresh_state_no_history(self):
        """Fresh state with no history → False."""
        state = {"signal_history": {}}
        result = is_whipsaw(state, "BTC", "LONG")
        assert result is False, "Fresh state should not trigger whipsaw"

    def test_whipsaw_same_direction_not_whipsaw(self):
        """Same direction twice → False."""
        state = {
            "signal_history": {
                "BTC": {"direction": "LONG", "ts": 1000.0}
            }
        }
        # Current time is much later, but same direction
        with patch('market_scanner.time.time', return_value=1010.0):
            result = is_whipsaw(state, "BTC", "LONG")
            assert result is False, "Same direction should not be whipsaw"

    def test_whipsaw_opposite_within_300s_is_whipsaw(self):
        """Opposite direction within 300s → True."""
        state = {
            "signal_history": {
                "BTC": {"direction": "LONG", "ts": 1000.0}
            }
        }
        # Switch to SHORT 10 seconds later
        with patch('market_scanner.time.time', return_value=1010.0):
            result = is_whipsaw(state, "BTC", "SHORT")
            assert result is True, "Opposite direction within 300s should be whipsaw"

    def test_whipsaw_opposite_after_300s_not_whipsaw(self):
        """Opposite direction after 300s → False (expired)."""
        state = {
            "signal_history": {
                "BTC": {"direction": "LONG", "ts": 1000.0}
            }
        }
        # Switch to SHORT 400 seconds later
        with patch('market_scanner.time.time', return_value=1400.0):
            result = is_whipsaw(state, "BTC", "SHORT")
            assert result is False, "Opposite direction after 300s should NOT be whipsaw"


# ─────────────────────────────────────────────────────────────
# F. suggest_tp_sl() with strategy-specific multipliers
# ─────────────────────────────────────────────────────────────

class TestSuggestTPSL:
    """Test TP/SL calculation with different strategy multipliers."""

    def test_tpsl_trend_pullback_default_rr(self, trending_up_1000):
        """Trend pullback (default): multiplier_sl=1.5, rr_ratio=2.0."""
        result = suggest_tp_sl(trending_up_1000, direction="LONG",
                               multiplier_sl=1.5, rr_ratio=2.0)

        # TP distance should be ~2x SL distance
        sl_dist = result["entry_price"] - result["suggested_sl"]
        tp_dist = result["suggested_tp"] - result["entry_price"]
        ratio = tp_dist / sl_dist if sl_dist > 0 else 0
        assert 1.9 < ratio < 2.1, f"Trend pullback R:R should be ~2.0, got {ratio}"

    def test_tpsl_breakout_multiplier(self, trending_up_1000):
        """Breakout: multiplier_sl=2.0, rr_ratio=2.5."""
        result = suggest_tp_sl(trending_up_1000, direction="LONG",
                               multiplier_sl=2.0, rr_ratio=2.5)

        # TP distance should be ~2.5x SL distance
        sl_dist = result["entry_price"] - result["suggested_sl"]
        tp_dist = result["suggested_tp"] - result["entry_price"]
        ratio = tp_dist / sl_dist if sl_dist > 0 else 0
        assert 2.4 < ratio < 2.6, f"Breakout R:R should be ~2.5, got {ratio}"

    def test_tpsl_mean_reversion_multiplier(self, trending_up_1000):
        """Mean reversion: multiplier_sl=1.0, rr_ratio=1.5."""
        result = suggest_tp_sl(trending_up_1000, direction="LONG",
                               multiplier_sl=1.0, rr_ratio=1.5)

        # TP distance should be ~1.5x SL distance
        sl_dist = result["entry_price"] - result["suggested_sl"]
        tp_dist = result["suggested_tp"] - result["entry_price"]
        ratio = tp_dist / sl_dist if sl_dist > 0 else 0
        assert 1.4 < ratio < 1.6, f"Mean reversion R:R should be ~1.5, got {ratio}"

    def test_tpsl_short_direction(self, trending_up_1000):
        """SHORT: TP < entry < SL."""
        result = suggest_tp_sl(trending_up_1000, direction="SHORT",
                               multiplier_sl=1.5, rr_ratio=2.0)

        assert result["suggested_tp"] < result["entry_price"], "SHORT TP should be below entry"
        assert result["suggested_sl"] > result["entry_price"], "SHORT SL should be above entry"
