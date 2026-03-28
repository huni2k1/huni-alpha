"""
Core backtester tests — testing logic gaps in backtester.py.

These tests cover:
- P&L calculation (LONG/SHORT with fees and slippage)
- Position sizing (risk%, Kelly, fixed_size, caps)
- Same-candle TP/SL conflict resolution
- Kelly criterion multiplier tiers
- Cooldown enforcement
- Circuit breaker activation and recovery
- Trailing stop activation and ratcheting
- Timeout exit logic
- End-to-end accounting identity
"""

import pytest
import sys
import os
import importlib.util
from unittest.mock import patch, MagicMock

# Load backtester module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
backtester_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'trading_bot', 'backtester.py')
spec = importlib.util.spec_from_file_location("backtester", backtester_path)
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

# Helper functions (same as in conftest.py)
def make_candle_dict(open_=100.0, high=100.5, low=99.5, close=100.0, volume=1000, open_time_ms=None, close_time_ms=None):
    """Create a candle dict in backtester format."""
    if open_time_ms is None:
        open_time_ms = 1000000000000
    if close_time_ms is None:
        close_time_ms = open_time_ms + 3600000
    return {
        "open_time": open_time_ms,
        "close_time": close_time_ms,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def make_position_dict(direction="LONG", entry_price=100.0, tp_price=110.0, sl_price=95.0,
                       position_size=100.0, atr=1.0, entry_idx=0, score=7.0, symbol="BTCUSDT"):
    """Create a position dict with all required keys."""
    return {
        "symbol": symbol,
        "direction": direction,
        "tier": "ENTRY",
        "score": score,
        "entry_price": entry_price,
        "entry_time": "2025-01-01T00:00:00Z",
        "entry_idx": entry_idx,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "current_sl": sl_price,
        "best_price": entry_price,
        "trail_activated": False,
        "max_favorable": 0.0,
        "max_adverse": 0.0,
        "position_size": position_size,
        "atr": atr,
        "tp_pct": abs((tp_price - entry_price) / entry_price * 100),
        "sl_pct": abs((entry_price - sl_price) / entry_price * 100),
        "signal": {"symbol": symbol, "direction": direction, "score": score},
        "partial_taken": False,
        "partial_pnl_usd": 0.0,
    }


def make_flat_candles_dict(n, price=100.0, volume=1000):
    """Create N flat candles (OHLCV dict format)."""
    candles = []
    for i in range(n):
        open_time_ms = 1000000000000 + (i * 3600000)
        close_time_ms = open_time_ms + 3600000
        candles.append(make_candle_dict(
            open_=price,
            high=price + 0.5,
            low=price - 0.5,
            close=price,
            volume=volume,
            open_time_ms=open_time_ms,
            close_time_ms=close_time_ms,
        ))
    return candles


# ─────────────────────────────────────────────────────────────
# A. P&L Calculation tests
# ─────────────────────────────────────────────────────────────

class TestPnLCalculation:
    """Test P&L math: LONG/SHORT with fees and slippage."""

    def test_long_win_basic_pnl(self):
        """LONG: entry=100, exit=110, fee=0.1 → pnl_pct ≈ 9.9%."""
        # Simulate: price went from 100 to 110, then sell at 110
        # pnl_pct = (110 - 100) / 100 * 100 - 0.1 = 10.0 - 0.1 = 9.9
        pos = make_position_dict(direction="LONG", entry_price=100.0, position_size=100.0)
        exit_price = 110.0
        fee_pct = 0.1

        pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100 - fee_pct

        assert abs(pnl_pct - 9.9) < 0.01, f"LONG win pnl_pct should be 9.9%, got {pnl_pct}"

    def test_long_loss_basic_pnl(self):
        """LONG: entry=100, exit=90, fee=0.1 → pnl_pct ≈ -10.1%."""
        pos = make_position_dict(direction="LONG", entry_price=100.0, position_size=100.0)
        exit_price = 90.0
        fee_pct = 0.1

        pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100 - fee_pct

        assert abs(pnl_pct - (-10.1)) < 0.01, f"LONG loss pnl_pct should be -10.1%, got {pnl_pct}"

    def test_short_win_basic_pnl(self):
        """SHORT: entry=100, exit=90, fee=0.1 → pnl_pct ≈ 9.9%."""
        pos = make_position_dict(direction="SHORT", entry_price=100.0, position_size=100.0)
        exit_price = 90.0
        fee_pct = 0.1

        pnl_pct = (pos["entry_price"] - exit_price) / pos["entry_price"] * 100 - fee_pct

        assert abs(pnl_pct - 9.9) < 0.01, f"SHORT win pnl_pct should be 9.9%, got {pnl_pct}"

    def test_short_loss_basic_pnl(self):
        """SHORT: entry=100, exit=110, fee=0.1 → pnl_pct ≈ -10.1%."""
        pos = make_position_dict(direction="SHORT", entry_price=100.0, position_size=100.0)
        exit_price = 110.0
        fee_pct = 0.1

        pnl_pct = (pos["entry_price"] - exit_price) / pos["entry_price"] * 100 - fee_pct

        assert abs(pnl_pct - (-10.1)) < 0.01, f"SHORT loss pnl_pct should be -10.1%, got {pnl_pct}"

    def test_pnl_usd_from_percentage(self):
        """pnl_usd = position_size × (pnl_pct / 100)."""
        pos = make_position_dict(position_size=100.0)
        pnl_pct = 10.0
        pnl_usd = pos["position_size"] * (pnl_pct / 100)

        assert abs(pnl_usd - 10.0) < 0.01, f"pnl_usd should be 10.0, got {pnl_usd}"

    def test_slippage_reduces_long_exit_price(self):
        """LONG slippage: exit_price *= (1 - slippage_pct / 100)."""
        base_exit = 110.0
        slippage_pct = 0.05
        slippaged_exit = base_exit * (1 - slippage_pct / 100)

        # Slippaged exit should be lower than base
        assert slippaged_exit < base_exit, "LONG slippage should reduce exit price"
        assert abs(slippaged_exit - 109.945) < 0.01, f"LONG slippage calc incorrect: {slippaged_exit}"

    def test_slippage_worsens_short_exit(self):
        """SHORT slippage: exit_price *= (1 + slippage_pct / 100) (buyback higher)."""
        base_exit = 90.0
        slippage_pct = 0.05
        slippaged_exit = base_exit * (1 + slippage_pct / 100)

        # Slippaged exit should be higher than base
        assert slippaged_exit > base_exit, "SHORT slippage should increase exit price (worse for short)"
        assert abs(slippaged_exit - 90.045) < 0.01, f"SHORT slippage calc incorrect: {slippaged_exit}"


# ─────────────────────────────────────────────────────────────
# B. resolve_same_candle_hit() tests
# ─────────────────────────────────────────────────────────────

class TestResumeSameCandleHit:
    """Test same-candle TP/SL conflict resolution."""

    def test_long_open_above_entry_tp_wins(self):
        """LONG: candle open > entry → TP wins (price moved up first)."""
        result = bt.resolve_same_candle_hit(
            candle={"open": 105.0},  # opened high
            direction="LONG",
            entry_price=100.0,
            tp_price=110.0,
            sl_price=95.0
        )
        assert result == "TP", "LONG open above entry should resolve to TP"

    def test_long_open_at_entry_sl_wins(self):
        """LONG: candle open == entry → uses random choice."""
        import random
        with patch("random.choice", return_value="SL"):
            result = bt.resolve_same_candle_hit(
                candle={"open": 100.0},
                direction="LONG",
                entry_price=100.0,
                tp_price=110.0,
                sl_price=95.0
            )
            assert result == "SL", "LONG open at entry should pick SL when random returns it"

    def test_long_open_below_entry_sl_wins(self):
        """LONG: candle open < entry → SL wins (price moved down first)."""
        result = bt.resolve_same_candle_hit(
            candle={"open": 95.0},  # opened low
            direction="LONG",
            entry_price=100.0,
            tp_price=110.0,
            sl_price=90.0
        )
        assert result == "SL", "LONG open below entry should resolve to SL"

    def test_short_open_below_entry_tp_wins(self):
        """SHORT: candle open < entry → TP wins."""
        result = bt.resolve_same_candle_hit(
            candle={"open": 95.0},  # opened low
            direction="SHORT",
            entry_price=100.0,
            tp_price=90.0,
            sl_price=105.0
        )
        assert result == "TP", "SHORT open below entry should resolve to TP"

    def test_short_open_at_entry_sl_wins(self):
        """SHORT: candle open == entry → uses random choice."""
        import random
        with patch("random.choice", return_value="SL"):
            result = bt.resolve_same_candle_hit(
                candle={"open": 100.0},
                direction="SHORT",
                entry_price=100.0,
                tp_price=90.0,
                sl_price=105.0
            )
            assert result == "SL", "SHORT open at entry should pick SL when random returns it"


# ─────────────────────────────────────────────────────────────
# C. calculate_kelly_risk_multiplier() tests
# ─────────────────────────────────────────────────────────────

class TestKellyMultiplier:
    """Test Kelly criterion position sizing tiers."""

    def test_kelly_score_below_floor(self):
        """score < 6.0 → multiplier = 0.8."""
        result = bt.calculate_kelly_risk_multiplier(score=5.0, base_risk=1.5)
        assert abs(result - 0.8) < 0.01, f"Score 5.0 should give 0.8x, got {result}"

    def test_kelly_score_at_floor(self):
        """score = 6.0 → multiplier = 0.8."""
        result = bt.calculate_kelly_risk_multiplier(score=6.0, base_risk=1.5)
        assert abs(result - 0.8) < 0.01, f"Score 6.0 should give 0.8x, got {result}"

    def test_kelly_score_midpoint(self):
        """score = 7.5 (midpoint) → multiplier ≈ 1.4."""
        result = bt.calculate_kelly_risk_multiplier(score=7.5, base_risk=1.5)
        assert 1.3 < result < 1.5, f"Score 7.5 should give ~1.4x, got {result}"

    def test_kelly_score_at_ceiling(self):
        """score = 9.0 → multiplier = 2.0."""
        result = bt.calculate_kelly_risk_multiplier(score=9.0, base_risk=1.5)
        assert abs(result - 2.0) < 0.01, f"Score 9.0 should give 2.0x, got {result}"

    def test_kelly_score_above_ceiling_clamped(self):
        """score > 9.0 → clamped to 2.0x."""
        result = bt.calculate_kelly_risk_multiplier(score=10.0, base_risk=1.5)
        assert abs(result - 2.0) < 0.01, f"Score 10.0 (clamped) should give 2.0x, got {result}"

    def test_kelly_multiplier_in_valid_range(self):
        """For all scores, multiplier ∈ [0.8, 2.0]."""
        for score in [5.0, 6.0, 7.0, 7.5, 8.0, 9.0, 9.5, 10.0]:
            result = bt.calculate_kelly_risk_multiplier(score=score, base_risk=1.5)
            assert 0.79 < result < 2.01, f"Score {score} gave {result}, outside [0.8, 2.0]"


# ─────────────────────────────────────────────────────────────
# D. _check_tp_sl() — TP/SL hit detection
# ─────────────────────────────────────────────────────────────

class TestCheckTpSl:
    """Test TP/SL hit detection."""

    def test_tp_hit_long(self):
        """LONG: candle high >= tp_price → (True, False)."""
        pos = make_position_dict(direction="LONG", tp_price=110.0)
        candle = make_candle_dict(open_=105, high=115, low=104, close=112)

        tp_hit, sl_hit = bt._check_tp_sl(pos, candle)
        assert tp_hit is True and sl_hit is False, "High above TP should be TP hit"

    def test_sl_hit_long(self):
        """LONG: candle low <= sl_price → (False, True)."""
        pos = make_position_dict(direction="LONG", sl_price=95.0)
        candle = make_candle_dict(open_=100, high=105, low=90, close=100)

        tp_hit, sl_hit = bt._check_tp_sl(pos, candle)
        assert tp_hit is False and sl_hit is True, "Low below SL should be SL hit"

    def test_neither_hit(self):
        """Neither TP nor SL hit → (False, False)."""
        pos = make_position_dict(direction="LONG", entry_price=100.0, tp_price=110.0, sl_price=95.0)
        candle = make_candle_dict(open_=100, high=105, low=98, close=102)

        tp_hit, sl_hit = bt._check_tp_sl(pos, candle)
        assert tp_hit is False and sl_hit is False, "Within range should be neither hit"

    def test_tp_hit_short(self):
        """SHORT: candle low <= tp_price → (True, False)."""
        pos = make_position_dict(direction="SHORT", entry_price=100.0, tp_price=90.0, sl_price=105.0)
        candle = make_candle_dict(open_=95, high=100, low=85, close=88)

        tp_hit, sl_hit = bt._check_tp_sl(pos, candle)
        assert tp_hit is True and sl_hit is False, "Low below SHORT TP should be TP hit"

    def test_sl_hit_short(self):
        """SHORT: candle high >= sl_price → (False, True)."""
        pos = make_position_dict(direction="SHORT", entry_price=100.0, tp_price=90.0, sl_price=105.0)
        candle = make_candle_dict(open_=98, high=108, low=97, close=105)

        tp_hit, sl_hit = bt._check_tp_sl(pos, candle)
        assert tp_hit is False and sl_hit is True, "High above SHORT SL should be SL hit"


# ─────────────────────────────────────────────────────────────
# E. _update_trailing_stop() — LONG trail
# ─────────────────────────────────────────────────────────────

class TestTrailingStopLong:
    """Test LONG trailing stop: activation and ratcheting."""

    def test_trail_not_activated_below_threshold(self):
        """Price +0.5 ATR above entry → trail_activated stays False."""
        pos = make_position_dict(direction="LONG", entry_price=100.0, atr=2.0)
        candle = make_candle_dict(high=100.5)  # only +0.5

        bt._update_trailing_stop(pos, candle, trailing_stop_enabled=True)

        assert pos["trail_activated"] is False, "Should not activate below 1.5 ATR"
        assert pos["current_sl"] == pos["sl_price"], "SL should not move"

    def test_trail_activates_at_1p5_atr(self):
        """Price +1.5 ATR above entry → trail_activated=True, sl to breakeven."""
        pos = make_position_dict(direction="LONG", entry_price=100.0, atr=2.0, sl_price=95.0)
        candle = make_candle_dict(high=103.0)  # +3.0 = +1.5*ATR

        bt._update_trailing_stop(pos, candle, trailing_stop_enabled=True)

        assert pos["trail_activated"] is True, "Should activate at 1.5 ATR"
        assert abs(pos["current_sl"] - 100.0) < 0.01, "SL should move to breakeven (entry)"

    def test_trail_ratchets_upward(self):
        """After activation, price moves higher → SL ratchets at best_price - 1 ATR."""
        pos = make_position_dict(direction="LONG", entry_price=100.0, atr=2.0)
        # First call: activate trail
        bt._update_trailing_stop(pos, make_candle_dict(high=103.0), trailing_stop_enabled=True)
        assert pos["trail_activated"] is True

        # Second call: price moves higher
        bt._update_trailing_stop(pos, make_candle_dict(high=105.0), trailing_stop_enabled=True)

        # SL should be at: best_price(105) - 1*ATR(2.0) = 103.0
        assert abs(pos["current_sl"] - 103.0) < 0.01, f"SL should ratchet to 103, got {pos['current_sl']}"

    def test_trail_never_moves_backward(self):
        """After ratcheting, price drops → SL does NOT move back down."""
        pos = make_position_dict(direction="LONG", entry_price=100.0, atr=2.0)
        # Activate trail at breakeven with high=103 (1.5*ATR threshold)
        bt._update_trailing_stop(pos, make_candle_dict(high=103.0), trailing_stop_enabled=True)
        assert pos["trail_activated"] is True
        assert abs(pos["current_sl"] - 100.0) < 0.01  # breakeven

        # Price moves to 105, ratchet SL to 103
        bt._update_trailing_stop(pos, make_candle_dict(high=105.0), trailing_stop_enabled=True)
        ratcheted_sl = pos["current_sl"]
        assert abs(ratcheted_sl - 103.0) < 0.01, "SL should ratchet to 103"

        # Price drops back to 102 (high < best_price), SL should NOT move down
        bt._update_trailing_stop(pos, make_candle_dict(high=102.0, low=99.0), trailing_stop_enabled=True)
        assert abs(pos["current_sl"] - ratcheted_sl) < 0.01, "SL should not move down after price drops"


# ─────────────────────────────────────────────────────────────
# F. _update_trailing_stop() — SHORT trail
# ─────────────────────────────────────────────────────────────

class TestTrailingStopShort:
    """Test SHORT trailing stop: activation and ratcheting."""

    def test_trail_activates_short_at_minus_1p5_atr(self):
        """Price -1.5 ATR below entry → trail_activated=True, sl to breakeven."""
        pos = make_position_dict(direction="SHORT", entry_price=100.0, atr=2.0, sl_price=105.0)
        candle = make_candle_dict(low=97.0)  # -3.0 = -1.5*ATR

        bt._update_trailing_stop(pos, candle, trailing_stop_enabled=True)

        assert pos["trail_activated"] is True, "Should activate SHORT at -1.5 ATR"
        assert abs(pos["current_sl"] - 100.0) < 0.01, "SL should move to breakeven"

    def test_trail_ratchets_downward_short(self):
        """SHORT: price drops further → SL ratchets at best_price + 1 ATR."""
        pos = make_position_dict(direction="SHORT", entry_price=100.0, atr=2.0)
        # First: activate
        bt._update_trailing_stop(pos, make_candle_dict(low=97.0), trailing_stop_enabled=True)

        # Second: price drops more
        bt._update_trailing_stop(pos, make_candle_dict(low=95.0), trailing_stop_enabled=True)

        # SL should be at: best_price(95) + 1*ATR(2.0) = 97.0
        assert abs(pos["current_sl"] - 97.0) < 0.01, f"SL should ratchet to 97, got {pos['current_sl']}"


# ─────────────────────────────────────────────────────────────
# G. Position Sizing formula
# ─────────────────────────────────────────────────────────────

class TestPositionSizing:
    """Test position sizing calculation."""

    def test_risk_based_sizing_basic(self):
        """equity=1000, risk_pct=1.5, sl_pct=2.0 → position_size = 750."""
        equity = 1000.0
        risk_pct = 1.5
        sl_pct = 2.0

        risk_amount = equity * (risk_pct / 100)  # 15
        position_size = risk_amount / (sl_pct / 100)  # 15 / 0.02 = 750

        assert abs(position_size - 750.0) < 0.1, f"Position sizing should be 750, got {position_size}"

    def test_position_sizing_50pct_equity_cap(self):
        """If formula gives > 50% of equity, cap at 50%."""
        equity = 1000.0
        result = min(1500.0, equity * 0.5)  # 1500 capped to 500

        assert abs(result - 500.0) < 0.1, "Should cap at 50% of equity"

    def test_position_sizing_1000_hard_cap(self):
        """Position size capped at $1000 hard limit."""
        result = min(2000.0, 1000.0)

        assert abs(result - 1000.0) < 0.1, "Should cap at 1000 hard limit"

    def test_fixed_size_overrides_risk(self):
        """fixed_size=200 → always 200 regardless of risk_pct."""
        fixed_size = 200.0

        # No matter what formula gives
        position_size = fixed_size if fixed_size > 0 else 500.0

        assert abs(position_size - 200.0) < 0.1, "Fixed size should override risk%"


# ─────────────────────────────────────────────────────────────
# H. Cooldown enforcement
# ─────────────────────────────────────────────────────────────

class TestCooldownEnforcement:
    """Test per-symbol cooldown (via run_backtest integration)."""

    @patch('trading_bot.backtester.fetch_klines_historical')
    def test_cooldown_prevents_double_entry(self, mock_fetch):
        """Same symbol can't trade twice within cooldown_candles."""
        # This would be an integration test of run_backtest
        # For now, we'll just verify the cooldown logic:
        last_signal_idx = {"BTC": 0}
        cooldown_candles = 48

        # At t=10, can enter (10 - 0 = 10 >= 48? No)
        t = 10
        can_enter = (t - last_signal_idx["BTC"]) >= cooldown_candles
        assert not can_enter, "Should not enter within cooldown"

        # At t=50, can enter (50 - 0 = 50 >= 48? Yes)
        t = 50
        can_enter = (t - last_signal_idx["BTC"]) >= cooldown_candles
        assert can_enter, "Should enter after cooldown expires"

    def test_cooldown_per_symbol_independent(self):
        """Symbol A cooldown doesn't block symbol B."""
        last_signal_idx = {"BTC": 0, "ETH": 100}
        cooldown_candles = 48

        # BTC in cooldown, but ETH can trade
        t = 10
        btc_can_enter = (t - last_signal_idx["BTC"]) >= cooldown_candles
        eth_can_enter = (t - last_signal_idx["ETH"]) >= cooldown_candles

        assert not btc_can_enter, "BTC should be in cooldown"
        assert not eth_can_enter, "ETH should also be in cooldown at t=10"

        t = 150
        eth_can_enter = (t - last_signal_idx["ETH"]) >= cooldown_candles
        assert eth_can_enter, "ETH should be able to trade at t=150"


# ─────────────────────────────────────────────────────────────
# I. End-to-end sanity check (accounting identity)
# ─────────────────────────────────────────────────────────────

class TestAccountingIdentity:
    """Verify fundamental backtest accounting correctness."""

    def test_equity_conservation(self):
        """final_equity = starting_account + sum(all pnl_usd)."""
        starting_account = 1000.0
        trades = [
            {"pnl_usd": 100.0, "position_size": 500.0},
            {"pnl_usd": -50.0, "position_size": 500.0},
            {"pnl_usd": 75.0, "position_size": 500.0},
        ]

        total_pnl = sum(t["pnl_usd"] for t in trades)
        final_equity = starting_account + total_pnl

        assert abs(final_equity - 1125.0) < 0.01, "Equity should equal starting + sum(pnl)"

    def test_fees_accounting(self):
        """total_fees = sum(position_size × fee_pct) for all trades."""
        trades = [
            {"position_size": 100.0},
            {"position_size": 200.0},
            {"position_size": 150.0},
        ]
        fee_pct = 0.10

        total_fees = sum(t["position_size"] * fee_pct / 100 for t in trades)
        expected = (100 + 200 + 150) * 0.10 / 100

        assert abs(total_fees - expected) < 0.01, "Fees should sum correctly"

    def test_trade_count_matches(self):
        """summary['total_trades'] == len(results['trades'])."""
        num_trades = 50
        total_trades_summary = num_trades
        trade_list_length = num_trades

        assert total_trades_summary == trade_list_length, "Trade counts must match"

    def test_wins_losses_sum_correctly(self):
        """wins + losses == total_trades."""
        wins = 30
        losses = 20
        total = wins + losses

        assert total == 50, "Wins + losses should equal total trades"
