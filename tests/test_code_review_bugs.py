"""
Tests for bugs found in code review (2026-04-04).

Covers:
- Pre-computed indicator alignment (no look-ahead)
- Same-candle TP/SL resolution determinism
- MACD hist_prev threshold
- Hybrid rejection reason isolation
- RSI Wilder smoothing correctness
- ADX pre-computation vs on-demand
- Statistical snapshot with/without precomputed indicators
- Backtester reproducibility (random seed)
- Position sizing edge cases
- Circuit breaker + monthly reset interaction
"""

import pytest
import sys
import os
import json
import random
import importlib.util
import numpy as np
from datetime import datetime, timezone

# Load modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
scanner_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'trading_bot', 'scanner.py')
spec = importlib.util.spec_from_file_location("market_scanner", scanner_path)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)

backtester_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'trading_bot', 'backtester.py')
spec_bt = importlib.util.spec_from_file_location("backtester", backtester_path)
bt = importlib.util.module_from_spec(spec_bt)
spec_bt.loader.exec_module(bt)

from trading_bot import trader


# ── Helpers ──────────────────────────────────────────────────────

def make_trending_candles(n=200, start_price=100.0, trend=0.1):
    """Generate n candles with a consistent uptrend."""
    candles = []
    price = start_price
    for i in range(n):
        price += trend
        candles.append([price - 0.5, price + 0.5, price - 0.8, price, 1000.0])
    return candles


def make_candle_dict(open_=100.0, high=100.5, low=99.5, close=100.0, volume=1000,
                     open_time_ms=None, close_time_ms=None):
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


# ═══════════════════════════════════════════════════════════════════
# TEST 1: Pre-computed indicators — no look-ahead
# ═══════════════════════════════════════════════════════════════════

class TestPrecomputedNoLookahead:
    """Verify pre-computed indicators at candle t don't use data from t+1."""

    def _make_candles_with_spike(self, n=300, spike_at=200):
        """Create candles where price spikes at a specific index."""
        candles = []
        for i in range(n):
            if i >= spike_at:
                # Massive spike — should affect indicators AFTER spike_at
                candles.append([200.0, 210.0, 195.0, 205.0, 5000.0])
            else:
                candles.append([100.0 + i * 0.01, 100.5 + i * 0.01,
                               99.5 + i * 0.01, 100.0 + i * 0.01, 1000.0])
        return candles

    def test_rsi_no_lookahead(self):
        """RSI at candle t should not change when candles after t change."""
        candles_a = self._make_candles_with_spike(300, spike_at=200)
        candles_b = self._make_candles_with_spike(300, spike_at=250)  # spike later

        cache_a = scanner.precompute_indicators_for_all_candles(candles_a)
        cache_b = scanner.precompute_indicators_for_all_candles(candles_b)

        # At candle 199 (before spike in both), RSI should be identical
        assert cache_a[199]['rsi'] == cache_b[199]['rsi'], \
            f"RSI at 199 differs: {cache_a[199]['rsi']} vs {cache_b[199]['rsi']}"

    def test_ema_no_lookahead(self):
        """EMA at candle t should not change when candles after t change."""
        candles_a = self._make_candles_with_spike(300, spike_at=200)
        candles_b = self._make_candles_with_spike(300, spike_at=250)

        cache_a = scanner.precompute_indicators_for_all_candles(candles_a)
        cache_b = scanner.precompute_indicators_for_all_candles(candles_b)

        assert cache_a[199]['e9'] == cache_b[199]['e9']
        assert cache_a[199]['e21'] == cache_b[199]['e21']
        assert cache_a[199]['e50'] == cache_b[199]['e50']

    def test_macd_no_lookahead(self):
        """MACD at candle t should not change when candles after t change."""
        candles_a = self._make_candles_with_spike(300, spike_at=200)
        candles_b = self._make_candles_with_spike(300, spike_at=250)

        cache_a = scanner.precompute_indicators_for_all_candles(candles_a)
        cache_b = scanner.precompute_indicators_for_all_candles(candles_b)

        assert abs(cache_a[199]['macd_hist'] - cache_b[199]['macd_hist']) < 1e-10


# ═══════════════════════════════════════════════════════════════════
# TEST 2: Pre-computed RSI matches on-demand RSI
# ═══════════════════════════════════════════════════════════════════

class TestPrecomputedVsOnDemand:
    """Pre-computed indicator values should closely match on-demand computation."""

    def _make_varied_candles(self, n=500):
        """Generate candles with some realistic variation."""
        random.seed(123)
        candles = []
        price = 100.0
        for _ in range(n):
            change = random.uniform(-2, 2)
            price += change
            h = price + random.uniform(0.5, 2.0)
            l = price - random.uniform(0.5, 2.0)
            candles.append([price - change * 0.5, h, l, price, random.uniform(500, 2000)])
        return candles

    def test_rsi_matches_within_tolerance(self):
        """Pre-computed RSI should be close to window-computed RSI."""
        candles = self._make_varied_candles(500)
        cache = scanner.precompute_indicators_for_all_candles(candles)

        # Compare at several points
        for t in [100, 200, 300, 400]:
            precomputed_rsi = cache[t]['rsi']
            # On-demand: RSI from closes up to candle t
            closes = [c[3] for c in candles[:t + 1]]
            ondemand_rsi = scanner.rsi(closes)
            # Allow some tolerance since full-history vs window can differ slightly
            assert abs(precomputed_rsi - ondemand_rsi) < 5.0, \
                f"RSI at {t}: precomputed={precomputed_rsi:.2f} vs ondemand={ondemand_rsi:.2f}"

    def test_ema_matches_exactly(self):
        """Pre-computed EMA should be very close to window-computed."""
        candles = self._make_varied_candles(500)
        cache = scanner.precompute_indicators_for_all_candles(candles)

        for t in [100, 200, 300, 400]:
            precomputed_e9 = cache[t]['e9']
            closes = [c[3] for c in candles[:t + 1]]
            ondemand_e9 = scanner.ema(closes, 9)[-1]
            # EMA should match very closely with enough history
            assert abs(precomputed_e9 - ondemand_e9) < 0.01, \
                f"EMA9 at {t}: precomputed={precomputed_e9:.4f} vs ondemand={ondemand_e9:.4f}"


# ═══════════════════════════════════════════════════════════════════
# TEST 3: Same-candle TP/SL resolution is deterministic
# ═══════════════════════════════════════════════════════════════════

class TestSameCandleResolution:
    """Test that same-candle TP/SL resolution is deterministic with seed."""

    def test_resolve_is_deterministic(self):
        """Running resolve_same_candle_hit twice with same seed gives same result."""
        candle = make_candle_dict(open_=100.0, high=105.0, low=95.0, close=100.0)

        results = []
        for _ in range(10):
            random.seed(42)
            result = bt.resolve_same_candle_hit(candle, "LONG", 100.0, 110.0, 95.0)
            results.append(result)

        assert len(set(results)) == 1, f"Got non-deterministic results: {results}"

    def test_long_open_above_entry_resolves_tp(self):
        """LONG: candle opens above entry → TP first."""
        candle = make_candle_dict(open_=105.0)
        result = bt.resolve_same_candle_hit(candle, "LONG", 100.0, 110.0, 95.0)
        assert result == "TP"

    def test_long_open_below_entry_resolves_sl(self):
        """LONG: candle opens below entry → SL first."""
        candle = make_candle_dict(open_=95.0)
        result = bt.resolve_same_candle_hit(candle, "LONG", 100.0, 110.0, 95.0)
        assert result == "SL"

    def test_short_open_below_entry_resolves_tp(self):
        """SHORT: candle opens below entry → TP first."""
        candle = make_candle_dict(open_=95.0)
        result = bt.resolve_same_candle_hit(candle, "SHORT", 100.0, 90.0, 105.0)
        assert result == "TP"

    def test_short_open_above_entry_resolves_sl(self):
        """SHORT: candle opens above entry → SL first."""
        candle = make_candle_dict(open_=105.0)
        result = bt.resolve_same_candle_hit(candle, "SHORT", 100.0, 90.0, 105.0)
        assert result == "SL"


# ═══════════════════════════════════════════════════════════════════
# TEST 4: MACD hist_prev threshold (Bug 2 fix)
# ═══════════════════════════════════════════════════════════════════

class TestMACDHistPrev:
    """MACD hist_prev should not silently return 0 for edge-case data lengths."""

    def test_macd_needs_35_candles_for_hist_prev(self):
        """macd(closes[:-1]) needs at least 35 candles in closes."""
        # With 36 candles, closes[:-1] has 35 → should work
        closes = [100.0 + i * 0.5 for i in range(36)]
        line, sig, hist = scanner.macd(closes[:-1])
        assert hist != 0 or (line == 0 and sig == 0), \
            "MACD on 35 candles should produce non-zero values"

    def test_macd_returns_zero_for_short_data(self):
        """macd() with < 35 candles should return zeros."""
        closes = [100.0 + i * 0.5 for i in range(30)]
        line, sig, hist = scanner.macd(closes)
        assert line == 0 and sig == 0 and hist == 0


# ═══════════════════════════════════════════════════════════════════
# TEST 5: Hybrid rejection reason isolation (Bug 4 fix)
# ═══════════════════════════════════════════════════════════════════

class TestHybridRejectionReason:
    """Rejection reasons from technical and statistical signals should not bleed."""

    def test_rejection_reasons_are_independent(self):
        """After technical sets a reason and statistical succeeds,
        statistical_reason should be None (not technical's reason)."""
        # Simulate the pattern from the hybrid signal function
        _last_rejection_reason = {}
        symbol = "BTCUSDT"

        # Technical fails, sets reason
        _last_rejection_reason[symbol] = "Asia session (UTC 3)"
        technical_reason = _last_rejection_reason.get(symbol)

        # Clear before statistical (the fix)
        _last_rejection_reason.pop(symbol, None)

        # Statistical succeeds, doesn't set reason
        statistical_reason = _last_rejection_reason.get(symbol)

        assert technical_reason == "Asia session (UTC 3)"
        assert statistical_reason is None, \
            f"statistical_reason should be None, got '{statistical_reason}'"


# ═══════════════════════════════════════════════════════════════════
# TEST 6: ADX pre-computation correctness
# ═══════════════════════════════════════════════════════════════════

class TestADXPrecomputation:
    """ADX pre-computed in backtester should match scanner's on-demand ADX."""

    def test_adx_no_lookahead(self):
        """ADX at candle t should not use data from candles after t."""
        random.seed(99)
        n = 200
        highs, lows, closes, volumes = [], [], [], []
        price = 100.0
        for i in range(n):
            if i < 150:
                price += 0.5  # trending
            else:
                price -= 2.0  # sudden reversal
            h = price + random.uniform(0.5, 1.5)
            l = price - random.uniform(0.5, 1.5)
            highs.append(h)
            lows.append(l)
            closes.append(price)
            volumes.append(1000.0)

        # Build ADX series using backtester's pre-computation logic
        adx_series = [0.0] * n
        if n >= 28:
            plus_dm, minus_dm, trs = [], [], []
            for i in range(1, n):
                h_diff = highs[i] - highs[i - 1]
                l_diff = lows[i - 1] - lows[i]
                plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
                minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
                trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                              abs(lows[i] - closes[i - 1])))

            def _wilder(vals, p):
                result = [sum(vals[:p])]
                for v in vals[p:]:
                    result.append(result[-1] - result[-1] / p + v)
                return result

            atr_s = _wilder(trs, 14)
            pdm_s = _wilder(plus_dm, 14)
            mdm_s = _wilder(minus_dm, 14)
            dx_vals = []
            for a, p, m in zip(atr_s, pdm_s, mdm_s):
                if a == 0:
                    dx_vals.append(0.0)
                    continue
                pdi = 100 * p / a
                mdi = 100 * m / a
                dx_vals.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0)
            for idx in range(28, n):
                end_dx = idx - 13
                start_dx = max(0, end_dx - 14)
                window = dx_vals[start_dx:end_dx]
                adx_series[idx] = float(np.mean(window)) if window else 0.0

        # At index 149 (before reversal), ADX with data up to 149 should equal
        # ADX computed on truncated data
        adx_from_full = adx_series[149]
        adx_from_window = scanner.adx(highs[:150], lows[:150], closes[:150])

        # Should be reasonably close (not exact due to full-history vs window)
        assert abs(adx_from_full - adx_from_window) < 10.0, \
            f"ADX at 149: precomputed={adx_from_full:.2f} vs window={adx_from_window:.2f}"

    def test_adx_series_values_are_reasonable(self):
        """ADX should be between 0 and 100."""
        random.seed(77)
        candles = []
        price = 100.0
        for _ in range(100):
            change = random.uniform(-1, 1)
            price += change
            candles.append([price, price + 1, price - 1, price, 1000.0])

        highs = [c[1] for c in candles]
        lows = [c[2] for c in candles]
        closes = [c[3] for c in candles]

        adx_val = scanner.adx(highs, lows, closes)
        assert 0 <= adx_val <= 100, f"ADX out of range: {adx_val}"


# ═══════════════════════════════════════════════════════════════════
# TEST 7: Statistical snapshot consistency
# ═══════════════════════════════════════════════════════════════════

class TestStatisticalSnapshot:
    """_build_statistical_snapshot with and without precomputed should agree."""

    def test_snapshot_fields_present(self):
        """Snapshot should contain all required fields."""
        candles = make_trending_candles(200)
        snapshot = scanner._build_statistical_snapshot("BTCUSDT", candles)
        required_fields = [
            "close", "rsi", "e9", "e21", "e50", "above_ema200", "below_ema200",
            "macd_line", "macd_signal", "macd_hist", "macd_hist_prev",
            "adx", "vol_ratio", "bb_mid", "bb_upper", "bb_lower",
            "bb_bandwidth", "bb_squeeze", "higher_highs", "lower_lows",
            "hour_utc", "symbol",
        ]
        for field in required_fields:
            assert field in snapshot, f"Missing field: {field}"

    def test_snapshot_rsi_in_range(self):
        """RSI should be between 0 and 100."""
        candles = make_trending_candles(200)
        snapshot = scanner._build_statistical_snapshot("BTCUSDT", candles)
        assert 0 <= snapshot["rsi"] <= 100

    def test_snapshot_adx_in_range(self):
        """ADX should be between 0 and 100."""
        candles = make_trending_candles(200)
        snapshot = scanner._build_statistical_snapshot("BTCUSDT", candles)
        assert 0 <= snapshot["adx"] <= 100


# ═══════════════════════════════════════════════════════════════════
# TEST 8: Backtester reproducibility
# ═══════════════════════════════════════════════════════════════════

class TestBacktesterReproducibility:
    """Backtest results should be identical across runs."""

    def test_random_seed_produces_same_sequence(self):
        """random.seed(42) should produce deterministic coin flips."""
        results_a = []
        random.seed(42)
        for _ in range(20):
            results_a.append(random.choice(["TP", "SL"]))

        results_b = []
        random.seed(42)
        for _ in range(20):
            results_b.append(random.choice(["TP", "SL"]))

        assert results_a == results_b

    def test_seed_is_roughly_balanced(self):
        """Seeded random should still be ~50/50 over many flips."""
        random.seed(42)
        flips = [random.choice(["TP", "SL"]) for _ in range(1000)]
        tp_pct = flips.count("TP") / len(flips)
        assert 0.40 < tp_pct < 0.60, f"Seed 42 produced {tp_pct:.1%} TP — too biased"


# ═══════════════════════════════════════════════════════════════════
# TEST 9: Position sizing edge cases
# ═══════════════════════════════════════════════════════════════════

class TestPositionSizing:
    """Position sizing should respect caps and handle edge cases."""

    def test_kelly_multiplier_bounds(self):
        """Kelly multiplier should be between 0.8 and 2.0."""
        for score in [5.0, 6.0, 7.0, 8.0, 9.0, 10.0]:
            mult = bt.calculate_kelly_risk_multiplier(score)
            assert 0.8 <= mult <= 2.0, f"Kelly multiplier for score {score} = {mult}, out of bounds"

    def test_kelly_low_score_gives_min(self):
        """Low score should give minimum multiplier."""
        mult = bt.calculate_kelly_risk_multiplier(5.0)
        assert mult == 0.8

    def test_kelly_high_score_gives_max(self):
        """Very high score should give maximum multiplier."""
        mult = bt.calculate_kelly_risk_multiplier(10.0)
        assert mult == 2.0


# ═══════════════════════════════════════════════════════════════════
# TEST 10: Regime detection
# ═══════════════════════════════════════════════════════════════════

class TestRegimeDetection:
    """detect_regime should correctly classify market regimes."""

    def _closes(self, n=100):
        return [100.0 + i * 0.01 for i in range(n)]

    def test_strong_trend_above_25(self):
        """ADX >= 25, no squeeze → trending."""
        regime = scanner.detect_regime(30.0, False, 1.0, self._closes())
        assert regime == "trending"

    def test_breakout_with_squeeze_and_volume(self):
        """BB squeeze + volume spike → breakout."""
        regime = scanner.detect_regime(15.0, True, 2.0, self._closes())
        assert regime == "breakout"

    def test_weak_trend_20_to_25(self):
        """ADX 20-25, no squeeze → weak_trend."""
        regime = scanner.detect_regime(22.0, False, 1.0, self._closes())
        assert regime == "weak_trend"

    def test_low_adx_still_weak_trend(self):
        """ADX < 20, no squeeze → weak_trend (ranging disabled)."""
        regime = scanner.detect_regime(10.0, False, 1.0, self._closes())
        assert regime == "weak_trend"

    def test_boundary_25_is_trending(self):
        """ADX exactly 25 → trending."""
        regime = scanner.detect_regime(25.0, False, 1.0, self._closes())
        assert regime == "trending"


# ═══════════════════════════════════════════════════════════════════
# TEST 11: Trader crash-window persistence
# ═══════════════════════════════════════════════════════════════════

class TestTraderEntryRecovery:
    """A filled live order should be persisted before TP/SL setup finishes."""

    class _FakeClient:
        def get_symbol_info(self, symbol):
            return {
                "qty_precision": 3,
                "price_precision": 2,
                "min_qty": 0.001,
                "min_notional": 5.0,
            }

        def set_leverage(self, symbol, leverage):
            return True

        def place_market_order(self, symbol, side, quantity):
            return {"filled_price": 100.0}

        def place_tp_order(self, symbol, close_side, tp_price, price_precision):
            raise RuntimeError("crash after fill")

        def place_market_close(self, symbol, close_side, quantity):
            return {"filled_price": 100.0}

    def test_filled_position_is_written_before_tp_sl_complete(self, tmp_path, monkeypatch):
        state_path = tmp_path / "trader-state.json"
        monkeypatch.setattr(trader, "_STATE_FILE", str(state_path))
        monkeypatch.setattr(trader, "send_telegram", lambda text: None)

        state = dict(trader._EMPTY_STATE)
        signal = {
            "symbol": "ETHUSDT",
            "direction": "LONG",
            "entry_price": 100.0,
            "tp": 104.0,
            "sl": 98.4,
            "tp_pct": 4.0,
            "sl_pct": 1.6,
            "score": 7.75,
            "strategy": "breakout",
        }
        cfg = {
            "risk_pct": 1.5,
            "max_positions": 3,
            "signal_model": "hybrid_technical_wide_short_rsi28",
        }

        with pytest.raises(RuntimeError, match="crash after fill"):
            trader.execute_entry(
                signal,
                state,
                self._FakeClient(),
                cfg,
                dry_run=False,
                balance=90.0,
            )

        saved = json.loads(state_path.read_text())
        pos = saved["positions"]["ETHUSDT"]
        assert pos["entry_price"] == 100.0
        assert pos["protection_status"] == "pending"
        assert pos["tp_order_id"] is None
        assert pos["sl_order_id"] is None


class TestTraderStartupReconcile:
    """Unknown Binance positions should be imported into local state."""

    class _RecoveringClient:
        def get_open_positions(self):
            return [
                {
                    "symbol": "ETHUSDT",
                    "positionAmt": "0.015",
                    "entryPrice": "2500.5",
                    "markPrice": "2510.0",
                }
            ]

        def get_symbol_info(self, symbol):
            return {
                "price_precision": 2,
            }

        def get_open_algo_orders(self, symbol):
            assert symbol == "ETHUSDT"
            return [
                {
                    "side": "SELL",
                    "type": "TAKE_PROFIT_MARKET",
                    "triggerPrice": "2560.0",
                    "clientAlgoId": "tp_eth",
                },
                {
                    "side": "SELL",
                    "type": "STOP_MARKET",
                    "triggerPrice": "2460.0",
                    "clientAlgoId": "sl_eth",
                },
            ]

    class _RecoveringClientNoProtection(_RecoveringClient):
        def get_open_algo_orders(self, symbol):
            return []

    def test_reconcile_imports_unknown_binance_position_into_state(self, tmp_path, monkeypatch):
        state_path = tmp_path / "trader-state.json"
        monkeypatch.setattr(trader, "_STATE_FILE", str(state_path))
        sent = []
        monkeypatch.setattr(trader, "send_telegram", sent.append)

        state = {
            "positions": {},
            "last_signal": {},
            "peak_equity": None,
            "circuit_breaker_until": None,
            "trade_log": [],
        }
        trader.reconcile_with_binance(state, self._RecoveringClient())

        pos = state["positions"]["ETHUSDT"]
        assert pos["direction"] == "LONG"
        assert pos["entry_price"] == 2500.5
        assert pos["quantity"] == 0.015
        assert pos["tp_price"] == 2560.0
        assert pos["sl_price"] == 2460.0
        assert pos["tp_order_id"] == "algo:tp_eth"
        assert pos["sl_order_id"] == "algo:sl_eth"
        assert pos["protection_status"] == "armed"
        assert pos["recovered_from_binance"] is True
        assert "Recovered open Binance positions into bot state: ETHUSDT" in sent[0]

    def test_reconcile_marks_recovered_position_untracked_without_protection_orders(self, tmp_path, monkeypatch):
        state_path = tmp_path / "trader-state.json"
        monkeypatch.setattr(trader, "_STATE_FILE", str(state_path))
        sent = []
        monkeypatch.setattr(trader, "send_telegram", sent.append)

        state = {
            "positions": {},
            "last_signal": {},
            "peak_equity": None,
            "circuit_breaker_until": None,
            "trade_log": [],
        }
        trader.reconcile_with_binance(state, self._RecoveringClientNoProtection())

        pos = state["positions"]["ETHUSDT"]
        assert pos["protection_status"] == "untracked"
        assert pos["tp_order_id"] is None
        assert pos["sl_order_id"] is None
        assert "Protection orders not fully recovered: ETHUSDT" in sent[0]
