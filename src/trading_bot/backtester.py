#!/usr/bin/env python3
"""
Backtester for Market Scanner V3 — Validates REAL signal performance.

Architecture: Calls generate_signal() from scanner.py (tech-only mode).
The backtester owns NOTHING about scoring or TP/SL computation.
It only does: fetch history → call generate_signal() → simulate fills → report stats.

This guarantees WYTIWYT: What You Test Is What You Trade.

Key features:
  - 1000-candle window for accurate EMA200
  - Trailing stop: breakeven at +1.5 ATR, then trail at 1x ATR
  - Round-trip fees subtracted (default 0.10%)
  - Multi-position tracking: max 3 concurrent trades
  - Whipsaw detection: 5-min direction lock to avoid choppy flips
  - Circuit breaker: pauses at 25% drawdown, resumes after recovery
  - Candle caching: disk-based cache for fast repeated backtests
  - Kelly Criterion sizing: dynamic position sizing (0.8x-2.0x based on signal confidence)
  - Asia session filter: skips low-liquidity hours (0-8 UTC)

Usage:
  python3 backtester.py                          # Default: 6mo, 10 symbols, trend_threshold=7.0, breakout_threshold=6.0
  python3 backtester.py --months 12              # 12 months
  python3 backtester.py --symbols BTCUSDT ETHUSDT
  python3 backtester.py --account 1000           # $1000 starting account
  python3 backtester.py --entry-threshold 7.0    # Stricter threshold
  python3 backtester.py --fee-pct 0.08           # Lower fees (maker orders)
  python3 backtester.py --trailing-stop           # Enable trailing stop (default: disabled)
  python3 backtester.py --output results.json    # Export JSON for dashboard

NOTE: Backtester runs in tech-only mode (no fundamentals/news) because:
  - Funding rates & L/S ratios: only current data available from Binance
  - News sentiment: RSS feeds are current-only, no historical archive
  - Fear & Greed: limited historical API, not worth the look-ahead risk
  This means live trading (with fundamentals+news) may differ slightly.
"""

import sys
import os
import json
import time
import random
import logging
import argparse
from collections import defaultdict
import numpy as np
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

# Import candle caching module (handle both module and script contexts)
try:
    from . import candle_cache
except ImportError:
    # Fallback for direct script execution
    _cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candle_cache.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("candle_cache", _cache_path)
    candle_cache = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candle_cache)

# ─────────────────────────────────────────────────────────────────
# SCANNER IMPORT — Suppress side effects, get pure functions
# ─────────────────────────────────────────────────────────────────
# Import RotatingFileHandler FIRST (before patching)
from logging.handlers import RotatingFileHandler as _orig_RFH

_orig_makedirs = os.makedirs
os.makedirs = lambda *a, **kw: None

_orig_FileHandler = logging.FileHandler
logging.FileHandler = lambda *a, **kw: logging.NullHandler()

# Now patch RotatingFileHandler to use NullHandler
import logging.handlers
logging.handlers.RotatingFileHandler = lambda *a, **kw: logging.NullHandler()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importlib.util
_scanner_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner", "market-scanner.py")
if not os.path.exists(_scanner_path):
    # Fallback: look for scanner.py in same directory
    _scanner_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner.py")
if not os.path.exists(_scanner_path):
    # Fallback: look for market-scanner.py in same directory
    _scanner_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market-scanner.py")

spec = importlib.util.spec_from_file_location("scanner", _scanner_path)
scanner = importlib.util.module_from_spec(spec)
scanner.os = os
scanner.logging = logging
scanner.sys = sys
spec.loader.exec_module(scanner)

# Restore patched stdlib
os.makedirs = _orig_makedirs
logging.FileHandler = _orig_FileHandler

# Import the functions we need — generate_signal is the key one
generate_signal = scanner.generate_signal
score_technical = scanner.score_technical
suggest_tp_sl   = scanner.suggest_tp_sl

# Our own logger
log = logging.getLogger("backtester")
log.setLevel(logging.INFO)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_sh)

# ─────────────────────────────────────────────────────────────────
# VALIDATE IMPORT CHAIN (WYTIWYT)
# ─────────────────────────────────────────────────────────────────
import inspect

def validate_code_match():
    """Ensure backtester uses live scanner functions."""
    assert callable(generate_signal), "CRITICAL: generate_signal is not callable"
    assert generate_signal.__module__ == "scanner", \
        f"CRITICAL: generate_signal module is {generate_signal.__module__}, not 'scanner'"

    sig = inspect.signature(generate_signal)
    assert "symbol" in sig.parameters, "CRITICAL: generate_signal missing 'symbol'"
    assert "candles_4h" in sig.parameters, "CRITICAL: generate_signal missing 'candles_4h'"
    assert "include_fundamentals" in sig.parameters, "CRITICAL: generate_signal missing 'include_fundamentals'"

validate_code_match()
log.info("✅ Code validation passed: Using scanner's generate_signal() (WYTIWYT)")

# ─────────────────────────────────────────────────────────────────
# IMPORT STRATEGY PARAMETERS FROM SCANNER
# ─────────────────────────────────────────────────────────────────
# Use scanner's optimized defaults; CLI args can override for backtesting
SCANNER_RISK_PCT = getattr(scanner, 'RISK_PER_TRADE_PCT', 2.0)
SCANNER_COOLDOWN = getattr(scanner, 'SIGNAL_COOLDOWN_CANDLES', 6)
SCANNER_TREND_THRESH = getattr(scanner, 'SIGNAL_THRESHOLD_TREND', 7.0)
SCANNER_BREAKOUT_THRESH = getattr(scanner, 'SIGNAL_THRESHOLD_BREAKOUT', 6.0)
SCANNER_MAX_POSITIONS = getattr(scanner, 'MAX_OPEN_POSITIONS', 3)
log.info(f"✅ Strategy parameters loaded from scanner: risk={SCANNER_RISK_PCT}%, cooldown={SCANNER_COOLDOWN}h, trend={SCANNER_TREND_THRESH}, breakout={SCANNER_BREAKOUT_THRESH}, max_pos={SCANNER_MAX_POSITIONS}")

# ─────────────────────────────────────────────────────────────────
# BINANCE HISTORICAL DATA FETCHER
# ─────────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CryptoBacktester/2.0"})
BACKTESTER_CACHE_VARIANT = "backtester_v2"
EXIT_REASON_ORDER = ["TP", "SL", "TRAIL_SL", "TIMEOUT"]
WEEKDAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def fetch_klines_historical(symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 1000, max_retries: int = 3) -> list:
    """Fetch klines in batches (Binance max 1000 per request) with retry logic."""
    all_candles = []
    current_start = start_ms

    while current_start < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ms,
            "limit": limit,
        }
        data = None
        for attempt in range(max_retries):
            try:
                r = SESSION.get("https://api.binance.com/api/v3/klines", params=params, timeout=15)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 1.0 * (attempt + 1)
                    log.warning(f"  Fetch error for {symbol} (attempt {attempt+1}/{max_retries}): {e}, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    log.error(f"  Fetch FAILED for {symbol} after {max_retries} attempts: {e}")

        if not data:
            break

        for c in data:
            all_candles.append({
                "open_time": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "close_time": int(c[6]),
            })

        current_start = int(data[-1][6]) + 1
        if len(data) < limit:
            break
        time.sleep(0.15)

    return all_candles


def validate_candle_completeness(symbol: str, candles: list, expected_hours: int) -> tuple:
    """
    Validate that fetched candles are complete and contiguous.

    Returns:
        (is_valid: bool, message: str)
    """
    if not candles:
        return False, f"{symbol}: No candles fetched"

    got = len(candles)
    # Allow 5% tolerance (some candles may be missing at edges)
    min_expected = int(expected_hours * 0.95)
    if got < min_expected:
        return False, f"{symbol}: Incomplete data — got {got} candles, expected ~{expected_hours} (min {min_expected})"

    # Check for gaps: each candle's open_time should be ~1h after the previous
    gap_count = 0
    max_gap_hours = 0
    for i in range(1, len(candles)):
        diff_ms = candles[i]["open_time"] - candles[i-1]["open_time"]
        diff_hours = diff_ms / 3_600_000
        if diff_hours > 1.5:  # More than 1.5x expected interval = gap
            gap_count += 1
            max_gap_hours = max(max_gap_hours, diff_hours)

    if gap_count > 0:
        msg = f"{symbol}: {gap_count} gap(s) detected in candle data (max gap: {max_gap_hours:.1f}h)"
        if max_gap_hours > 24:
            return False, msg + " — gap > 24h, data unreliable"
        else:
            log.warning(f"  {msg}")

    return True, f"{symbol}: OK ({got} candles, {gap_count} minor gaps)"


def fetch_klines_historical_cached(symbol: str, interval: str, start_ms: int,
                                  end_ms: int, use_cache: bool = True) -> list:
    """
    Fetch historical klines with local disk caching.

    For backtesting: caches entire date ranges to avoid repeated API calls.
    Massive speed improvement on repeated backtests.

    Args:
        symbol: Trading pair
        interval: Candle interval ("1h", "4h")
        start_ms: Start time (milliseconds since epoch)
        end_ms: End time (milliseconds since epoch)
        use_cache: Enable caching (default: True)

    Returns:
        List of candle dicts with open_time, open, high, low, close, volume, close_time
    """
    if not use_cache:
        return fetch_klines_historical(symbol, interval, start_ms, end_ms)

    # Convert milliseconds to date strings
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")

    # Try to load from the backtester cache variant, which preserves exchange timestamps.
    cached_candles = candle_cache.load_from_cache(
        symbol,
        interval,
        start_str,
        end_str,
        variant=BACKTESTER_CACHE_VARIANT,
    )
    if cached_candles is not None:
        if not cached_candles:
            log.info(f"  {symbol}: Loaded 0 candles from cache ({start_str}–{end_str})")
            return cached_candles

        first_candle = cached_candles[0]
        required_keys = {"open_time", "open", "high", "low", "close", "volume", "close_time"}
        if isinstance(first_candle, dict) and required_keys.issubset(first_candle):
            log.info(f"  {symbol}: Loaded {len(cached_candles)} candles from cache ({start_str}–{end_str})")
            return cached_candles

        log.warning(
            f"  {symbol}: Ignoring legacy cache without timestamps for {start_str}–{end_str}; refetching"
        )

    # Not in cache — fetch from API
    log.info(f"  {symbol}: Cache miss, fetching from Binance API ({start_str}–{end_str})...")
    candles = fetch_klines_historical(symbol, interval, start_ms, end_ms)

    if candles:
        # Save the full candle dicts so cached backtests preserve exchange timestamps and gaps.
        candle_cache.save_to_cache(
            symbol,
            interval,
            start_str,
            end_str,
            candles,
            variant=BACKTESTER_CACHE_VARIANT,
        )
        log.info(f"  {symbol}: Cached {len(candles)} candles")

    return candles


# ─────────────────────────────────────────────────────────────────
# SAME-CANDLE TP/SL RESOLUTION
# ─────────────────────────────────────────────────────────────────
def resolve_same_candle_hit(candle: dict, direction: str,
                            entry_price: float, tp_price: float, sl_price: float) -> str:
    """
    When both TP and SL are hit in the same candle, determine which hit first.

    Strategy: Use the candle open to infer direction of first move.
    - If open is closer to SL side → likely SL hit first (moved against us initially)
    - If open is closer to TP side → likely TP hit first (moved in our favor initially)
    - If ambiguous → random (honest: we can't know intra-candle order)

    This removes the systematic bias of the old distance-based approach
    which always favored SL (since SL is always closer than TP at 3:1 R:R).
    """
    candle_open = candle["open"]

    if direction == "LONG":
        # For LONG: TP is above entry, SL is below entry
        # If candle opened above entry → price moved up first → TP likely first
        # If candle opened below entry → price moved down first → SL likely first
        if candle_open > entry_price:
            return "TP"
        elif candle_open < entry_price:
            return "SL"
        else:
            return random.choice(["TP", "SL"])
    else:
        # For SHORT: TP is below entry, SL is above entry
        if candle_open < entry_price:
            return "TP"
        elif candle_open > entry_price:
            return "SL"
        else:
            return random.choice(["TP", "SL"])


# ─────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS FOR POSITION MANAGEMENT
# ─────────────────────────────────────────────────────────────────
def _update_trailing_stop(pos: dict, candle: dict, trailing_stop_enabled: bool):
    """
    Update trailing stop logic for an open position.
    Updates in-place: current_sl, best_price, trail_activated.
    """
    if not trailing_stop_enabled or pos["atr"] <= 0:
        return

    direction = pos["direction"]
    atr_val = pos["atr"]

    if direction == "LONG":
        if candle["high"] > pos["best_price"]:
            pos["best_price"] = candle["high"]
        # Activate trail once price moves 1.5x ATR above entry
        if pos["best_price"] >= pos["entry_price"] + 1.5 * atr_val:
            if not pos["trail_activated"]:
                # First activation: move SL to breakeven
                pos["current_sl"] = pos["entry_price"]
                pos["trail_activated"] = True
            else:
                # Trail at 1x ATR below best price
                trail_sl = pos["best_price"] - 1.0 * atr_val
                if trail_sl > pos["current_sl"]:
                    pos["current_sl"] = trail_sl
    else:  # SHORT
        if candle["low"] < pos["best_price"]:
            pos["best_price"] = candle["low"]
        if pos["best_price"] <= pos["entry_price"] - 1.5 * atr_val:
            if not pos["trail_activated"]:
                pos["current_sl"] = pos["entry_price"]
                pos["trail_activated"] = True
            else:
                trail_sl = pos["best_price"] + 1.0 * atr_val
                if trail_sl < pos["current_sl"]:
                    pos["current_sl"] = trail_sl


def _check_tp_sl(pos: dict, candle: dict) -> tuple:
    """
    Check if TP or SL is hit for a position.
    Returns (tp_hit: bool, sl_hit: bool).
    """
    direction = pos["direction"]
    if direction == "LONG":
        sl_hit = candle["low"] <= pos["current_sl"]
        tp_hit = candle["high"] >= pos["tp_price"]
    else:  # SHORT
        sl_hit = candle["high"] >= pos["current_sl"]
        tp_hit = candle["low"] <= pos["tp_price"]
    return tp_hit, sl_hit


def _update_excursion(pos: dict, candle: dict):
    """
    Track max favorable and adverse price movement.
    Updates in-place: max_favorable, max_adverse.
    """
    direction = pos["direction"]
    if direction == "LONG":
        fav = (candle["high"] - pos["entry_price"]) / pos["entry_price"] * 100
        adv = (pos["entry_price"] - candle["low"]) / pos["entry_price"] * 100
    else:  # SHORT
        fav = (pos["entry_price"] - candle["low"]) / pos["entry_price"] * 100
        adv = (candle["high"] - pos["entry_price"]) / pos["entry_price"] * 100
    pos["max_favorable"] = max(pos["max_favorable"], fav)
    pos["max_adverse"] = max(pos["max_adverse"], adv)


def _close_position(pos: dict, exit_price: float, exit_reason: str, exit_candle: dict,
                   current_equity: float, equity_curve: list, all_trades: list,
                   sym_trades_map: dict, fee_pct: float, t: int,
                   slippage_pct: float = 0.0) -> float:
    """
    Close an open position: compute P&L, build trade dict, update equity.
    Returns updated current_equity.
    """
    symbol = pos["symbol"]
    direction = pos["direction"]
    entry_price = pos["entry_price"]
    entry_time = pos["entry_time"]
    entry_idx = pos["entry_idx"]

    # Apply adverse slippage to exit price
    if slippage_pct > 0:
        if direction == "LONG":
            exit_price *= (1 - slippage_pct / 100)  # Sell lower
        else:
            exit_price *= (1 + slippage_pct / 100)  # Buy back higher

    # Calculate P&L
    if direction == "LONG":
        pnl_pct = (exit_price - entry_price) / entry_price * 100
    else:  # SHORT
        pnl_pct = (entry_price - exit_price) / entry_price * 100
    pnl_pct -= fee_pct  # Subtract round-trip fee

    # Calculate position P&L in USD
    # For partial TP trades: pos["position_size"] is already halved; combine with the earlier partial close
    pnl_usd = pos["position_size"] * (pnl_pct / 100)
    partial_pnl_usd = pos.get("partial_pnl_usd", 0.0)
    total_pnl_usd = pnl_usd + partial_pnl_usd
    # Blended pnl_pct uses the original full position size
    original_position_size = pos["position_size"] * (2.0 if pos.get("partial_taken") else 1.0)
    total_pnl_pct = total_pnl_usd / original_position_size * 100 if original_position_size > 0 else pnl_pct

    # EXIT: Release locked capital and add P&L
    current_equity += pos["position_size"]  # Release locked money
    current_equity += pnl_usd                # Add profit/loss (partial TP already included)

    # Prevent liquidation
    if current_equity < 0:
        current_equity = 0

    # Position duration
    position_duration_hours = t - entry_idx if t >= entry_idx else 0

    exit_time = datetime.fromtimestamp(exit_candle["close_time"] / 1000, tz=timezone.utc)

    # Build trade dict
    trade = {
        "symbol": symbol,
        "direction": direction,
        "strategy": pos["signal"].get("strategy", pos["signal"]["details"].get("strategy", "")),
        "regime": pos["signal"].get("regime", pos["signal"]["details"].get("regime", "")),
        "tier": pos["tier"],
        "score": round(pos["score"], 2),
        "entry_price": round(entry_price, 6),
        "entry_time": entry_time.isoformat(),
        "exit_price": round(exit_price, 6),
        "exit_time": exit_time.isoformat(),
        "duration_hours": position_duration_hours,
        "exit_reason": exit_reason,
        "tp_price": round(pos["tp_price"], 6),
        "sl_price": round(pos["sl_price"], 6),
        "final_sl": round(pos["current_sl"], 6),
        "trail_activated": pos["trail_activated"],
        "tp_pct": round(pos["tp_pct"], 2),
        "sl_pct": round(pos["sl_pct"], 2),
        "fee_pct": fee_pct,
        "atr": pos["atr"],
        "pnl_pct": round(total_pnl_pct, 2),
        "pnl_usd": round(total_pnl_usd, 2),
        "position_size": round(original_position_size, 2),
        "equity_after": round(current_equity, 2),
        "max_favorable_pct": round(pos["max_favorable"], 2),
        "max_adverse_pct": round(pos["max_adverse"], 2),
        "details": {
            "rsi": pos["signal"]["details"].get("rsi", {}).get("1h"),
            "adx": pos["signal"]["details"].get("adx"),
            "ema_bull": pos["signal"]["details"].get("ema_bull"),
            "ema_bear": pos["signal"]["details"].get("ema_bear"),
            "above_ema200": pos["signal"]["details"].get("above_e200"),
            "vol_ratio": pos["signal"]["details"].get("vol_ratio"),
            "regime": pos["signal"].get("regime", pos["signal"]["details"].get("regime", "")),
            "strategy": pos["signal"].get("strategy", pos["signal"]["details"].get("strategy", "")),
        }
    }

    all_trades.append(trade)
    if symbol not in sym_trades_map:
        sym_trades_map[symbol] = []
    sym_trades_map[symbol].append(trade)

    # NOTE: Equity curve recording moved to periodic snapshots in main loop
    # (every 24 candles) using total_equity (free + locked + unrealized).
    # Recording free cash here would create false drawdowns when positions are open.

    return current_equity


# ─────────────────────────────────────────────────────────────────
# KELLY CRITERION — Dynamic Position Sizing
# ─────────────────────────────────────────────────────────────────
def calculate_kelly_risk_multiplier(score: float) -> float:
    """
    Calculate dynamic risk multiplier based on technical signal score.

    Only applies to technical signal modes where score varies per candle (6.0–9.5).
    Statistical mode bypasses this entirely and uses flat risk_pct.

    Scaling:
    - Score 6.0 (min entry): 0.8x multiplier
    - Score 6.5: 1.0x multiplier (baseline)
    - Score 7.0: 1.3x multiplier
    - Score 8.0: 1.7x multiplier
    - Score 9.0+: 2.0x multiplier (half-Kelly equivalent)
    """
    if score >= 9.0:
        return 2.0
    if score <= 6.0:
        return 0.8
    norm = (score - 6.0) / 3.0
    return 0.8 + (norm * 1.2)


def _calculate_streaks(trades: list) -> tuple:
    max_wins = max_losses = 0
    current_wins = current_losses = 0
    for trade in trades:
        if trade["pnl_pct"] > 0:
            current_wins += 1
            current_losses = 0
        elif trade["pnl_pct"] < 0:
            current_losses += 1
            current_wins = 0
        else:
            current_wins = 0
            current_losses = 0
        max_wins = max(max_wins, current_wins)
        max_losses = max(max_losses, current_losses)
    return max_wins, max_losses


def _annualize_return(total_return_pct: float, months_tested: int) -> float:
    if months_tested <= 0:
        return 0.0

    period_return = total_return_pct / 100
    if period_return <= -1.0:
        return -100.0

    annualized_return = ((1.0 + period_return) ** (12 / months_tested) - 1.0) * 100
    return float(annualized_return)


def _calculate_risk_metrics(monthly_sorted: list, total_return_pct: float,
                            max_drawdown_pct: float, months_tested: int) -> dict:
    monthly_returns = [m["monthly_return_pct"] for m in monthly_sorted]
    calendar_months = len(monthly_returns)
    effective_months = max(months_tested, calendar_months)

    if effective_months > calendar_months:
        monthly_returns = monthly_returns + [0.0] * (effective_months - calendar_months)

    positive_months = len([r for r in monthly_returns if r > 0])
    negative_months = len([r for r in monthly_returns if r < 0])
    monthly_vol = float(np.std(monthly_returns, ddof=0)) if monthly_returns else 0.0
    downside = [r for r in monthly_returns if r < 0]
    downside_vol = float(np.std(downside, ddof=0)) if downside else 0.0
    avg_monthly = float(np.mean(monthly_returns)) if monthly_returns else 0.0
    annualized_return_pct = _annualize_return(total_return_pct, effective_months)

    sharpe = 0.0
    if monthly_vol > 0:
        sharpe = (avg_monthly / monthly_vol) * np.sqrt(12)

    sortino = 0.0
    if downside_vol > 0:
        sortino = (avg_monthly / downside_vol) * np.sqrt(12)

    calmar = (annualized_return_pct / max_drawdown_pct) if max_drawdown_pct > 0 else 0.0
    recovery_factor = (total_return_pct / max_drawdown_pct) if max_drawdown_pct > 0 else 0.0
    consistency_pct = (positive_months / effective_months * 100) if effective_months > 0 else 0.0

    return {
        "months_tested": effective_months,
        "requested_months": months_tested,
        "positive_months": positive_months,
        "negative_months": negative_months,
        "consistency_pct": round(consistency_pct, 1),
        "avg_monthly_return_pct": round(avg_monthly, 2),
        "monthly_volatility_pct": round(monthly_vol, 2),
        "downside_volatility_pct": round(downside_vol, 2),
        "best_month_return_pct": round(max(monthly_returns), 2) if monthly_returns else 0,
        "worst_month_return_pct": round(min(monthly_returns), 2) if monthly_returns else 0,
        "annualized_return_pct": round(float(annualized_return_pct), 2),
        "sharpe_ratio": round(float(sharpe), 2),
        "sortino_ratio": round(float(sortino), 2),
        "calmar_ratio": round(float(calmar), 2),
        "recovery_factor": round(float(recovery_factor), 2),
    }


def _aggregate_trade_stats(trades: list) -> dict:
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "total_pnl_usd": 0,
            "avg_pnl_usd": 0,
            "avg_pnl_pct": 0,
            "profit_factor": 0,
            "avg_score": 0,
            "avg_duration_hours": 0,
            "avg_mfe_pct": 0,
            "avg_mae_pct": 0,
            "avg_r_multiple": 0,
        }

    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] < 0]
    gross_profit = sum(t["pnl_usd"] for t in wins)
    gross_loss = abs(sum(t["pnl_usd"] for t in losses))
    r_multiples = [t["pnl_pct"] / t["sl_pct"] for t in trades if t.get("sl_pct", 0) > 0]

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "total_pnl_usd": round(sum(t["pnl_usd"] for t in trades), 2),
        "avg_pnl_usd": round(float(np.mean([t["pnl_usd"] for t in trades])), 2),
        "avg_pnl_pct": round(float(np.mean([t["pnl_pct"] for t in trades])), 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0,
        "avg_score": round(float(np.mean([t["score"] for t in trades])), 2),
        "avg_duration_hours": round(float(np.mean([t.get("duration_hours", 0) for t in trades])), 1),
        "avg_mfe_pct": round(float(np.mean([t.get("max_favorable_pct", 0) for t in trades])), 2),
        "avg_mae_pct": round(float(np.mean([t.get("max_adverse_pct", 0) for t in trades])), 2),
        "avg_r_multiple": round(float(np.mean(r_multiples)), 2) if r_multiples else 0,
    }


def _parse_trade_timestamp(timestamp_str: str) -> datetime:
    normalized = timestamp_str.replace("Z", "+00:00") if timestamp_str.endswith("Z") else timestamp_str
    return datetime.fromisoformat(normalized)


def _build_trade_breakdowns(trades: list) -> dict:
    buckets = {
        "by_strategy": defaultdict(list),
        "by_regime": defaultdict(list),
        "by_exit_reason": defaultdict(list),
        "by_entry_hour_utc": defaultdict(list),
        "by_entry_weekday_utc": defaultdict(list),
    }

    for trade in trades:
        strategy = trade.get("strategy") or trade.get("details", {}).get("strategy", "unknown")
        regime = trade.get("regime") or trade.get("details", {}).get("regime", "unknown")
        exit_reason = trade.get("exit_reason", "UNKNOWN")
        entry_dt = _parse_trade_timestamp(trade["entry_time"])
        hour_key = f"{entry_dt.hour:02d}"
        weekday_key = entry_dt.strftime("%a")

        buckets["by_strategy"][strategy].append(trade)
        buckets["by_regime"][regime].append(trade)
        buckets["by_exit_reason"][exit_reason].append(trade)
        buckets["by_entry_hour_utc"][hour_key].append(trade)
        buckets["by_entry_weekday_utc"][weekday_key].append(trade)

    def build_sorted_dict(bucket_map: dict, sorter) -> dict:
        items = []
        for key, bucket_trades in bucket_map.items():
            items.append((key, _aggregate_trade_stats(bucket_trades)))
        items.sort(key=sorter)
        return {key: value for key, value in items}

    return {
        "by_strategy": build_sorted_dict(
            buckets["by_strategy"],
            sorter=lambda item: (-item[1]["total_pnl_usd"], item[0]),
        ),
        "by_regime": build_sorted_dict(
            buckets["by_regime"],
            sorter=lambda item: (-item[1]["total_pnl_usd"], item[0]),
        ),
        "by_exit_reason": build_sorted_dict(
            buckets["by_exit_reason"],
            sorter=lambda item: (
                EXIT_REASON_ORDER.index(item[0]) if item[0] in EXIT_REASON_ORDER else len(EXIT_REASON_ORDER),
                item[0],
            ),
        ),
        "by_entry_hour_utc": build_sorted_dict(
            buckets["by_entry_hour_utc"],
            sorter=lambda item: int(item[0]),
        ),
        "by_entry_weekday_utc": build_sorted_dict(
            buckets["by_entry_weekday_utc"],
            sorter=lambda item: (
                WEEKDAY_ORDER.index(item[0]) if item[0] in WEEKDAY_ORDER else len(WEEKDAY_ORDER),
                item[0],
            ),
        ),
    }


# ─────────────────────────────────────────────────────────────────
# BACKTESTER CORE
# ─────────────────────────────────────────────────────────────────
def run_backtest(
    symbols: list,
    months: int = 6,
    account: float = 1000.0,
    risk_pct: float = 1.5,
    threshold_entry: float = 5.0,
    threshold_high: float = 8.0,
    cooldown_candles: int = 48,  # 1H candles: 48 candles = 48 hours
    fee_pct: float = 0.10,
    trailing_stop: bool = False,
    reset_monthly: bool = False,
    # New parameters
    trend_threshold: float = 0.0,       # Strategy-specific: trend_pullback min score (0=use threshold_entry)
    breakout_threshold: float = 0.0,    # Strategy-specific: breakout min score (0=use threshold_entry)
    max_positions: int = 3,             # Max concurrent open positions
    slippage_pct: float = 0.05,         # Adverse slippage per side (%)
    use_next_open: bool = True,         # Entry at next candle open (more realistic)
    fixed_size: float = 0.0,            # Fixed $ per trade (0=use risk% sizing)
    max_drawdown_pct: float = 25.0,     # Circuit breaker: pause at this drawdown %
    recovery_candles: int = 168,        # Circuit breaker: candles to wait before resuming
    partial_tp: bool = False,           # Close 50% at 1R, move SL to breakeven (disabled: degrades performance)
    kelly_sizing: bool = False,         # Kelly Criterion-based dynamic position sizing
    signal_model: str = "technical",    # Signal engine mode: technical (default) or statistical
    validated_setups_path: Optional[str] = None,  # Deduped validated_setups export for statistical mode
) -> dict:
    """
    Walk-forward backtest using the scanner's generate_signal() contract.

    For each 1h candle:
    1. Take the last 1000 candles as the window (accurate EMA200)
    2. Call generate_signal() in the selected signal model
    3. If score >= threshold, simulate a trade using the signal's TP/SL
    4. Walk forward with trailing stop: once +1.5 ATR, trail at breakeven then 1x ATR
    5. Subtract round-trip fees from each trade

    TP/SL come from the signal (ATR-based), NOT from CLI args.
    """

    end_dt = datetime.now(timezone.utc)
    warmup_days = 170  # Extra days so 1000-candle window has full EMA200 warmup
    start_dt = end_dt - timedelta(days=30 * months + warmup_days)

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_trades = []
    equity_curve = [{"time": start_dt.isoformat(), "equity": account}]
    current_equity = account
    monthly_start_equity = {}
    current_calendar_month = None
    symbol_stats = {}

    # ── STEP 1: Pre-fetch all candles ──────────────────────────────
    step1_start = time.time()
    log.info(f"STEP 1: Fetching candles for {len(symbols)} symbols ({months}mo + warmup)...")
    all_candles = {}
    warmup_hours = warmup_days * 24
    window_size = 1000
    trade_start_idx = max(window_size, warmup_hours)

    expected_hours = months * 30 * 24 + warmup_hours  # Total expected candles
    skipped_symbols = []

    for symbol in symbols:
        sym_fetch_start = time.time()
        # Use cached version for faster repeated backtests
        candles = fetch_klines_historical_cached(symbol, "1h", start_ms, end_ms, use_cache=True)
        fetch_time = time.time() - sym_fetch_start
        log.info(f"  {symbol}: Fetched {len(candles)} candles in {fetch_time:.2f}s")

        if len(candles) < 1010:
            log.warning(f"    {symbol}: Not enough data ({len(candles)} < 1010), skipping")
            skipped_symbols.append((symbol, f"only {len(candles)} candles"))
            continue

        # Validate completeness and contiguity
        sym_validate_start = time.time()
        is_valid, msg = validate_candle_completeness(symbol, candles, expected_hours)
        validate_time = time.time() - sym_validate_start
        if not is_valid:
            log.error(f"    {symbol}: DATA REJECTED: {msg}")
            skipped_symbols.append((symbol, msg))
            continue
        log.info(f"    {symbol}: ✅ {msg} ({validate_time:.2f}s)")

        all_candles[symbol] = candles

    step1_time = time.time() - step1_start
    log.info(f"STEP 1 completed in {step1_time:.2f}s")

    if skipped_symbols:
        log.warning(f"Skipped {len(skipped_symbols)}/{len(symbols)} symbols due to data issues:")
        for sym, reason in skipped_symbols:
            log.warning(f"  - {sym}: {reason}")

    if not all_candles:
        log.error("No symbols with sufficient data, exiting.")
        return {
            "config": {},
            "summary": {
                "total_trades": 0,
                "final_equity": account,
                "total_pnl_usd": 0,
            },
            "monthly": [],
            "by_symbol": {},
            "equity_curve": equity_curve,
            "trades": [],
        }

    # Resolve strategy-specific thresholds (0 = use global threshold_entry)
    _trend_thresh = trend_threshold if trend_threshold > 0 else threshold_entry
    _breakout_thresh = breakout_threshold if breakout_threshold > 0 else threshold_entry

    # ── STEP 2: Time-ordered loop across all symbols ───────────────
    max_candles = max(len(candles) for candles in all_candles.values())
    step2_start = time.time()
    log.info(f"STEP 2: Running backtest loop ({max_candles - trade_start_idx} candles)...")
    open_positions = []  # List of active position dicts
    last_signal_idx = {sym: -999 for sym in all_candles.keys()}
    sym_trades_map = {}  # {symbol: [trades]}
    peak_equity = account
    circuit_breaker_until = -1  # Candle index when trading resumes

    # Timing counters
    signal_count = 0
    position_close_count = 0
    rejection_counts = {}  # Track rejection reasons for summary
    entry_count = 0

    for t in range(trade_start_idx, max_candles):
        # Log progress every 500 candles
        if (t - trade_start_idx) % 500 == 0 and t > trade_start_idx:
            elapsed = time.time() - step2_start
            progress_pct = (t - trade_start_idx) / (max_candles - trade_start_idx) * 100
            log.info(f"  Progress: {progress_pct:.1f}% ({t - trade_start_idx}/{max_candles - trade_start_idx} candles, {elapsed:.1f}s elapsed)")
        # ── STEP A: Update/close open positions at candle t ────────
        for pos in open_positions[:]:  # iterate over copy
            symbol = pos["symbol"]
            if t >= len(all_candles[symbol]):
                continue

            candle = all_candles[symbol][t]

            # Update trailing stop
            _update_trailing_stop(pos, candle, trailing_stop)

            # Handle partial TP at 1R (close 50%, move SL to breakeven on remainder)
            if partial_tp and not pos.get("partial_taken"):
                entry = pos["entry_price"]
                sl_distance = abs(entry - pos["sl_price"])

                if pos["direction"] == "LONG":
                    target_1r = entry + sl_distance
                    hit_1r = candle["high"] >= target_1r
                else:  # SHORT
                    target_1r = entry - sl_distance
                    hit_1r = candle["low"] <= target_1r

                if hit_1r:
                    # Close 50% at 1R
                    half_size = pos["position_size"] * 0.5
                    if pos["direction"] == "LONG":
                        partial_pnl_pct = (target_1r - entry) / entry * 100
                    else:
                        partial_pnl_pct = (entry - target_1r) / entry * 100
                    partial_pnl_pct -= fee_pct / 2  # Half fee for half position

                    partial_pnl_usd = half_size * (partial_pnl_pct / 100)
                    current_equity += half_size + partial_pnl_usd

                    # Update position: reduce size and move SL to entry (breakeven)
                    pos["position_size"] *= 0.5
                    pos["current_sl"] = entry
                    pos["partial_taken"] = True
                    pos["partial_pnl_usd"] = partial_pnl_usd

            # Check TP/SL (for remaining position if partial was taken)
            tp_hit, sl_hit = _check_tp_sl(pos, candle)

            # Update excursion
            _update_excursion(pos, candle)

            # Resolve exit
            exit_price = None
            exit_reason = None

            if sl_hit and tp_hit:
                # Both in same candle — resolve using open-based logic
                exit_reason = resolve_same_candle_hit(candle, pos["direction"],
                                                     pos["entry_price"],
                                                     pos["tp_price"],
                                                     pos["current_sl"])
                exit_price = pos["tp_price"] if exit_reason == "TP" else pos["current_sl"]
                if exit_reason == "SL" and pos["trail_activated"]:
                    exit_reason = "TRAIL_SL"
            elif sl_hit:
                exit_price = pos["current_sl"]
                exit_reason = "TRAIL_SL" if pos["trail_activated"] else "SL"
            elif tp_hit:
                exit_price = pos["tp_price"]
                exit_reason = "TP"

            # Check timeout (180 candles = 7.5 days)
            if exit_price is None and (t - pos["entry_idx"]) >= 180:
                exit_price = candle["close"]
                exit_reason = "TIMEOUT"

            # Close if needed
            if exit_price is not None:
                current_equity = _close_position(pos, exit_price, exit_reason, candle,
                                                current_equity, equity_curve, all_trades,
                                                sym_trades_map, fee_pct, t, slippage_pct)
                open_positions.remove(pos)
                position_close_count += 1

        # ── TRUE EQUITY TRACKING (includes unrealized P&L) ────────
        # current_equity is free cash only. Compute total equity = free + locked + unrealized
        locked_capital = sum(p["position_size"] for p in open_positions)
        unrealized_pnl = 0.0
        for pos in open_positions:
            sym = pos["symbol"]
            if t < len(all_candles[sym]):
                mark_price = all_candles[sym][t]["close"]
                if pos["direction"] == "LONG":
                    unrealized_pnl += pos["position_size"] * ((mark_price - pos["entry_price"]) / pos["entry_price"])
                else:
                    unrealized_pnl += pos["position_size"] * ((pos["entry_price"] - mark_price) / pos["entry_price"])
        total_equity = current_equity + locked_capital + unrealized_pnl

        # Update peak and check circuit breaker
        if total_equity > peak_equity:
            peak_equity = total_equity
        current_dd_pct = (peak_equity - total_equity) / peak_equity * 100 if peak_equity > 0 else 0

        candle_month = None
        for _sym in all_candles:
            if t < len(all_candles[_sym]):
                candle_dt = datetime.fromtimestamp(all_candles[_sym][t]["close_time"] / 1000, tz=timezone.utc)
                candle_month = candle_dt.strftime("%Y-%m")
                break
        if candle_month is not None and candle_month != current_calendar_month:
            if reset_monthly and current_calendar_month is not None:
                # Monthly reset: circuit breaker from last month doesn't carry over.
                # Sizing already uses flat `account` value (not current_equity), so
                # a bad month doesn't shrink position sizes in the next month.
                circuit_breaker_until = -1
            current_calendar_month = candle_month
            monthly_start_equity.setdefault(candle_month, round(total_equity, 2))

        if current_dd_pct >= max_drawdown_pct and max_drawdown_pct < 100:
            if t >= circuit_breaker_until:  # Not already paused
                circuit_breaker_until = t + recovery_candles
                log.warning(f"CIRCUIT BREAKER: {current_dd_pct:.1f}% drawdown at candle {t}, pausing {recovery_candles} candles")

        # Record daily equity snapshot (every 24 candles) with true equity
        if t % 24 == 0:
            # Find a timestamp from any available symbol at this candle
            for _sym in all_candles:
                if t < len(all_candles[_sym]):
                    snap_time = datetime.fromtimestamp(all_candles[_sym][t]["close_time"] / 1000, tz=timezone.utc)
                    equity_curve.append({"time": snap_time.isoformat(), "equity": round(total_equity, 2)})
                    break

        # ── STEP B: Check for new entries at candle t ──────────────
        # Skip entries if circuit breaker active or liquidated
        if t < circuit_breaker_until or current_equity <= 0:
            if current_equity <= 0 and not any(open_positions):
                log.warning(f"Account liquidated at ${current_equity:.2f}, stopping trades")
            continue  # Skip to next candle

        # Skip entries if at max concurrent positions
        if len(open_positions) >= max_positions:
            continue

        for symbol in all_candles.keys():
            if t >= len(all_candles[symbol]):
                continue

            # Max positions check (could fill up during this symbol loop)
            if len(open_positions) >= max_positions:
                break

            # Cooldown check
            if (t - last_signal_idx[symbol]) < cooldown_candles:
                continue

            # Stop if liquidated
            if current_equity <= 0:
                break

            candles = all_candles[symbol]

            # For next-candle-open entry, need t+1 to exist
            if use_next_open and (t + 1) >= len(candles):
                continue

            # Build candle window (up to and including candle t)
            window_candles = []
            for j in range(max(0, t - window_size), t + 1):
                c = candles[j]
                window_candles.append([c["open"], c["high"], c["low"], c["close"], c["volume"]])

            # ── Generate signal ──
            # Pass candle's timestamp so generate_signal applies Asia filter correctly
            candle_time = datetime.fromtimestamp(
                candles[t]["close_time"] / 1000, tz=timezone.utc
            )

            # NOTE: Indicator caching is currently DISABLED because pre-computed indicators
            # are based on full history, not sliding windows. This causes misalignment.
            # Proper fix: either (1) pre-compute within window, or (2) use caching only for
            # non-window-sensitive calculations. For now, just compute fresh for each window.
            # indicators_at_t = all_indicators.get(symbol, {}).get(t)

            try:
                signal = generate_signal(symbol, window_candles,
                                         include_fundamentals=False,
                                         include_news=False,
                                         current_time=candle_time,
                                         signal_model=signal_model,
                                         validated_setups_path=validated_setups_path)
                                         # precomputed_indicators=indicators_at_t)
            except Exception as exc:
                raise RuntimeError(
                    f"generate_signal failed for {symbol} at {candle_time.isoformat()} "
                    f"using signal_model={signal_model}"
                ) from exc

            if signal is None:
                reasons = getattr(scanner, '_last_rejection_reason', {})
                reason = reasons.get(symbol, 'unknown')
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                continue

            signal_count += 1
            score = signal["score"]
            direction = signal["direction"]

            if direction == "NEUTRAL":
                continue

            # Strategy-specific threshold filtering.
            # Statistical mode uses validated setup membership as the primary gate.
            strategy = signal.get("strategy", "trend_pullback")
            if signal_model in {"statistical", "statistical_curated", "statistical_wide_short_rsi28"}:
                required_threshold = None
            elif signal_model == "hybrid_technical_wide_short_rsi28":
                required_threshold = None if signal.get("strategy") == "statistical_wide_short_rsi28" else (
                    _breakout_thresh if "breakout" in strategy else _trend_thresh
                )
            elif "breakout" in strategy:
                required_threshold = _breakout_thresh
            else:
                required_threshold = _trend_thresh

            if required_threshold is not None and score < required_threshold:
                continue

            # Determine tier
            if signal_model in {"statistical", "statistical_curated", "statistical_wide_short_rsi28"}:
                tier = "ENTRY"
            elif signal_model == "hybrid_technical_wide_short_rsi28":
                tier = "ENTRY" if signal.get("strategy") == "statistical_wide_short_rsi28" else (
                    "HIGH_CONF" if score >= threshold_high else "ENTRY"
                )
            elif score >= threshold_high:
                tier = "HIGH_CONF"
            else:
                tier = "ENTRY"

            # Get signal info
            signal_entry = signal["entry_price"]
            tp_price = signal["tp"]
            sl_price = signal["sl"]
            tp_pct_sig = signal["tp_pct"]
            sl_pct_sig = signal["sl_pct"]
            atr_val = signal.get("atr", 0)

            # Entry price: use next candle open (realistic) or signal close (legacy)
            if use_next_open:
                entry_idx = t + 1
                entry_candle = candles[entry_idx]
                entry_price = entry_candle["open"]
                # Re-anchor TP/SL from actual entry using same ATR distances
                # Compute distances from signal and apply to new entry
                if direction == "LONG":
                    tp_distance = tp_price - signal_entry
                    sl_distance = signal_entry - sl_price
                    tp_price = entry_price + tp_distance
                    sl_price = entry_price - sl_distance
                else:
                    tp_distance = signal_entry - tp_price
                    sl_distance = sl_price - signal_entry
                    tp_price = entry_price - tp_distance
                    sl_price = entry_price + sl_distance
            else:
                entry_idx = t
                entry_candle = candles[entry_idx]
                entry_price = signal_entry

            # Apply slippage (adverse direction)
            if slippage_pct > 0:
                if direction == "LONG":
                    entry_price *= (1 + slippage_pct / 100)
                else:
                    entry_price *= (1 - slippage_pct / 100)

            if use_next_open:
                entry_time = datetime.fromtimestamp(entry_candle["open_time"] / 1000, tz=timezone.utc)
            else:
                entry_time = datetime.fromtimestamp(entry_candle["close_time"] / 1000, tz=timezone.utc)

            # Position sizing
            if fixed_size > 0:
                position_size = fixed_size
            else:
                if reset_monthly:
                    equity_for_sizing = account
                else:
                    equity_for_sizing = current_equity

                # Dynamic risk based on Kelly Criterion (technical mode only).
                # Statistical mode uses flat risk_pct — setup validation is the quality gate.
                effective_risk_pct = risk_pct
                is_statistical = signal_model in {"statistical", "statistical_curated", "statistical_wide_short_rsi28"}
                if kelly_sizing and not is_statistical:
                    kelly_multiplier = calculate_kelly_risk_multiplier(score)
                    effective_risk_pct = risk_pct * kelly_multiplier

                risk_amount = equity_for_sizing * (effective_risk_pct / 100)
                position_size = risk_amount / (sl_pct_sig / 100) if sl_pct_sig > 0 else risk_amount
                position_size = min(position_size, equity_for_sizing * 0.5)

            # Capital constraint: check if we have enough total equity (free + locked + unrealized P&L)
            # This prevents liquidation when positions are underwater
            unrealized_pnl = 0
            for pos in open_positions:
                sym = pos["symbol"]
                if t < len(all_candles[sym]):
                    current_price = all_candles[sym][t]["close"]  # Use each position's own symbol price
                else:
                    current_price = pos["entry_price"]  # Fallback: assume no P&L if candle not available
                if pos["direction"] == "LONG":
                    price_change_pct = (current_price - pos["entry_price"]) / pos["entry_price"]
                else:  # SHORT
                    price_change_pct = (pos["entry_price"] - current_price) / pos["entry_price"]
                unrealized_pnl += pos["position_size"] * price_change_pct

            locked_capital = sum(pos["position_size"] for pos in open_positions)
            total_equity = current_equity + locked_capital + unrealized_pnl
            safe_equity = total_equity * 0.8  # 20% safety buffer

            if safe_equity < position_size:
                continue

            # ENTRY: Lock capital
            current_equity -= position_size

            # Create position dict
            pos = {
                "symbol": symbol,
                "direction": direction,
                "tier": tier,
                "score": score,
                "entry_price": entry_price,
                "entry_time": entry_time,
                "entry_idx": entry_idx,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "current_sl": sl_price,
                "best_price": entry_price,
                "trail_activated": False,
                "max_favorable": 0.0,
                "max_adverse": 0.0,
                "position_size": position_size,
                "atr": atr_val,
                "tp_pct": tp_pct_sig,
                "sl_pct": sl_pct_sig,
                "signal": signal,
                "partial_taken": False,
                "partial_pnl_usd": 0.0,
            }
            open_positions.append(pos)
            last_signal_idx[symbol] = t
            entry_count += 1

    step2_time = time.time() - step2_start
    log.info(f"STEP 2 completed in {step2_time:.2f}s")
    log.info(f"  - Signals generated: {signal_count}")
    log.info(f"  - Entries created: {entry_count}")
    log.info(f"  - Positions closed: {position_close_count}")
    if rejection_counts:
        log.info(f"  - Rejection breakdown (total {sum(rejection_counts.values())}):")
        for reason, count in sorted(rejection_counts.items(), key=lambda x: -x[1]):
            log.info(f"      {count:5d}  {reason}")

    # ── STEP 3: Force-close remaining positions at end of data ─────
    step3_start = time.time()
    for pos in open_positions:
        symbol = pos["symbol"]
        candles = all_candles[symbol]
        last_candle = candles[-1]
        current_equity = _close_position(pos, last_candle["close"], "TIMEOUT", last_candle,
                                         current_equity, equity_curve, all_trades,
                                         sym_trades_map, fee_pct, max_candles - 1, slippage_pct)
    step3_time = time.time() - step3_start
    log.info(f"STEP 3 (force-close remaining) completed in {step3_time:.2f}s")

    # ── Build per-symbol stats ──────────────────────────────────────
    step4_start = time.time()
    for symbol, trades in sym_trades_map.items():
        if trades:
            wins = [t for t in trades if t["pnl_pct"] > 0]
            losses = [t for t in trades if t["pnl_pct"] <= 0]
            symbol_stats[symbol] = {
                "total_trades": len(trades),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
                "avg_win_pct": round(np.mean([t["pnl_pct"] for t in wins]), 2) if wins else 0,
                "avg_loss_pct": round(np.mean([t["pnl_pct"] for t in losses]), 2) if losses else 0,
                "total_pnl_usd": round(sum(t["pnl_usd"] for t in trades), 2),
                "best_trade": round(max(t["pnl_pct"] for t in trades), 2),
                "worst_trade": round(min(t["pnl_pct"] for t in trades), 2),
            }

    # ── Aggregate stats ──────────────────────────────────────────
    wins = [t for t in all_trades if t["pnl_pct"] > 0]
    losses = [t for t in all_trades if t["pnl_pct"] <= 0]

    gross_profit = sum(t["pnl_usd"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl_usd"] for t in losses)) if losses else 0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    # ── Score Band Analysis ──────────────────────────────────────
    # Break down performance by signal confidence levels
    score_bands = {}
    for lower, upper in [(5.0, 5.5), (5.5, 6.0), (6.0, 6.5), (6.5, 7.0), (7.0, 8.0)]:
        band_key = f"{lower:.1f}-{upper:.1f}"
        band_trades = [t for t in all_trades if lower <= t["score"] < upper]
        if band_trades:
            band_wins = len([t for t in band_trades if t["pnl_pct"] > 0])
            band_loss = len([t for t in band_trades if t["pnl_pct"] <= 0])
            band_pnl = sum(t["pnl_usd"] for t in band_trades)
            band_avg_pnl_pct = round(np.mean([t["pnl_pct"] for t in band_trades]), 2)
            score_bands[band_key] = {
                "trades": len(band_trades),
                "wins": band_wins,
                "losses": band_loss,
                "win_rate": round(band_wins / len(band_trades) * 100, 1) if band_trades else 0,
                "pnl_usd": round(band_pnl, 2),
                "avg_win_pct": round(np.mean([t["pnl_pct"] for t in band_trades if t["pnl_pct"] > 0]), 2) if [t for t in band_trades if t["pnl_pct"] > 0] else 0,
                "avg_loss_pct": round(np.mean([t["pnl_pct"] for t in band_trades if t["pnl_pct"] <= 0]), 2) if [t for t in band_trades if t["pnl_pct"] <= 0] else 0,
                "avg_pnl_pct": band_avg_pnl_pct,
            }

    # Max drawdown
    peak = account
    max_dd = 0
    max_dd_pct = 0
    for point in equity_curve:
        eq = point["equity"]
        if eq > peak:
            peak = eq
        dd = peak - eq
        dd_pct = dd / peak * 100
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd = dd

    # Monthly breakdown
    monthly = {}

    for t in all_trades:
        month_key = t["entry_time"][:7]
        if month_key not in monthly:
            monthly[month_key] = {"trades": 0, "wins": 0, "pnl_usd": 0, "pnl_pct_sum": 0}
        monthly[month_key]["trades"] += 1
        monthly[month_key]["pnl_usd"] += t["pnl_usd"]
        monthly[month_key]["pnl_pct_sum"] += t["pnl_pct"]
        if t["pnl_pct"] > 0:
            monthly[month_key]["wins"] += 1

    monthly_sorted = []
    for k in sorted(monthly.keys()):
        m = monthly[k]
        month_start_equity = monthly_start_equity.get(k, account)
        monthly_return_pct = (m["pnl_usd"] / month_start_equity * 100) if month_start_equity > 0 else 0
        monthly_sorted.append({
            "month": k,
            "trades": m["trades"],
            "wins": m["wins"],
            "win_rate": round(m["wins"] / m["trades"] * 100, 1) if m["trades"] > 0 else 0,
            "pnl_usd": round(m["pnl_usd"], 2),
            "month_start_equity": round(month_start_equity, 2),
            "monthly_return_pct": round(monthly_return_pct, 2),
            "avg_pnl_pct": round(m["pnl_pct_sum"] / m["trades"], 2) if m["trades"] > 0 else 0,
        })

    # Rolling returns (1M, 3M, 6M, 12M from start of period, forward in time)
    rolling_returns = {}

    # Helper to calculate return from first N months (forward from start)
    def calc_rolling_return(n_months):
        if len(monthly_sorted) == 0:
            return {"pnl_usd": 0, "return_pct": 0, "months_available": 0, "months_requested": n_months}

        # Take first n_months from monthly_sorted (forward from start, not backward from end)
        months_slice = monthly_sorted[:n_months] if n_months <= len(monthly_sorted) else monthly_sorted

        total_pnl = sum(m["pnl_usd"] for m in months_slice)
        # Return percentage is: total_pnl / starting_account * 100
        return_pct = (total_pnl / account * 100) if account > 0 else 0

        return {
            "pnl_usd": round(total_pnl, 2),
            "return_pct": round(return_pct, 2),
            "months_available": len(months_slice),
            "months_requested": n_months,
        }

    rolling_returns["1m"] = calc_rolling_return(1)
    rolling_returns["3m"] = calc_rolling_return(3)
    rolling_returns["6m"] = calc_rolling_return(6)
    rolling_returns["12m"] = calc_rolling_return(12)

    # Direction breakdown
    long_trades = [t for t in all_trades if t["direction"] == "LONG"]
    short_trades = [t for t in all_trades if t["direction"] == "SHORT"]
    avg_win_usd = float(np.mean([t["pnl_usd"] for t in wins])) if wins else 0.0
    avg_loss_usd = float(np.mean([abs(t["pnl_usd"]) for t in losses])) if losses else 0.0
    r_multiples = [t["pnl_pct"] / t["sl_pct"] for t in all_trades if t.get("sl_pct", 0) > 0]
    win_r = [t["pnl_pct"] / t["sl_pct"] for t in wins if t.get("sl_pct", 0) > 0]
    loss_r = [t["pnl_pct"] / t["sl_pct"] for t in losses if t.get("sl_pct", 0) > 0]
    avg_mfe_pct = float(np.mean([t.get("max_favorable_pct", 0) for t in all_trades])) if all_trades else 0.0
    avg_mae_pct = float(np.mean([t.get("max_adverse_pct", 0) for t in all_trades])) if all_trades else 0.0
    max_consecutive_wins, max_consecutive_losses = _calculate_streaks(all_trades)

    # TP/SL distribution stats
    all_tp_pcts = [t["tp_pct"] for t in all_trades]
    all_sl_pcts = [t["sl_pct"] for t in all_trades]

    by_strategy_direction = {}
    strategy_direction_keys = sorted(
        {(t["strategy"], t["direction"]) for t in all_trades},
        key=lambda x: (x[0], x[1]),
    )
    for strategy, direction in strategy_direction_keys:
        key = f"{strategy}_{direction}"
        bucket_trades = [
            t for t in all_trades
            if t["strategy"] == strategy and t["direction"] == direction
        ]
        by_strategy_direction[key] = _aggregate_trade_stats(bucket_trades)

    total_pnl_usd = round(sum(m["pnl_usd"] for m in monthly_sorted), 2) if reset_monthly else round(current_equity - account, 2)
    total_return_pct = round((sum(m["pnl_usd"] for m in monthly_sorted) / account * 100), 2) if reset_monthly else round((current_equity - account) / account * 100, 2)
    risk_metrics = _calculate_risk_metrics(monthly_sorted, total_return_pct, round(max_dd_pct, 2), months)
    trade_breakdowns = _build_trade_breakdowns(all_trades)

    results = {
        "config": {
            "symbols": symbols,
            "months": months,
            "starting_account": account,
            "risk_pct": risk_pct,
            "fee_pct": fee_pct,
            "slippage_pct": slippage_pct,
            "trailing_stop": trailing_stop,
            "reset_monthly": reset_monthly,
            "tp_sl_source": "ATR-based from generate_signal() (strategy-dependent R:R)",
            "avg_tp_pct": round(np.mean(all_tp_pcts), 2) if all_tp_pcts else 0,
            "avg_sl_pct": round(np.mean(all_sl_pcts), 2) if all_sl_pcts else 0,
            "threshold_entry": threshold_entry,
            "trend_threshold": _trend_thresh,
            "breakout_threshold": _breakout_thresh,
            "threshold_high": threshold_high,
            "signal_model": signal_model,
            "validated_setups_path": validated_setups_path,
            "cooldown_candles": cooldown_candles,
            "max_positions": max_positions,
            "use_next_open": use_next_open,
            "fixed_size": fixed_size,
            "max_drawdown_pct": max_drawdown_pct,
            "scoring": f"multi_regime ({signal_model})",
            "run_time": datetime.now(timezone.utc).isoformat(),
        },
        "summary": {
            "total_trades": len(all_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(all_trades) * 100, 1) if all_trades else 0,
            "profit_factor": profit_factor,
            "total_pnl_usd": total_pnl_usd,
            "total_return_pct": total_return_pct,
            "final_equity": round(account + sum(m["pnl_usd"] for m in monthly_sorted), 2) if reset_monthly else round(current_equity, 2),
            "max_drawdown_usd": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "avg_win_pct": round(np.mean([t["pnl_pct"] for t in wins]), 2) if wins else 0,
            "avg_loss_pct": round(np.mean([t["pnl_pct"] for t in losses]), 2) if losses else 0,
            "avg_win_usd": round(avg_win_usd, 2),
            "avg_loss_usd": round(avg_loss_usd, 2),
            "payoff_ratio": round(avg_win_usd / avg_loss_usd, 2) if avg_loss_usd > 0 else 0,
            "avg_ev_per_trade_pct": round(np.mean([t["pnl_pct"] for t in all_trades]), 2) if all_trades else 0,
            "avg_ev_per_trade_usd": round(np.mean([t["pnl_usd"] for t in all_trades]), 2) if all_trades else 0,
            "best_trade_pct": round(max(t["pnl_pct"] for t in all_trades), 2) if all_trades else 0,
            "worst_trade_pct": round(min(t["pnl_pct"] for t in all_trades), 2) if all_trades else 0,
            "avg_mfe_pct": round(avg_mfe_pct, 2),
            "avg_mae_pct": round(avg_mae_pct, 2),
            "avg_r_multiple": round(float(np.mean(r_multiples)), 2) if r_multiples else 0,
            "avg_win_r": round(float(np.mean(win_r)), 2) if win_r else 0,
            "avg_loss_r": round(float(np.mean(loss_r)), 2) if loss_r else 0,
            "max_consecutive_wins": max_consecutive_wins,
            "max_consecutive_losses": max_consecutive_losses,
            "avg_trades_per_month": round(len(all_trades) / max(months, 1), 1),
            "long_trades": len(long_trades),
            "short_trades": len(short_trades),
            "long_win_rate": round(len([t for t in long_trades if t["pnl_pct"] > 0]) / len(long_trades) * 100, 1) if long_trades else 0,
            "short_win_rate": round(len([t for t in short_trades if t["pnl_pct"] > 0]) / len(short_trades) * 100, 1) if short_trades else 0,
            "tp_exits": len([t for t in all_trades if t["exit_reason"] == "TP"]),
            "sl_exits": len([t for t in all_trades if t["exit_reason"] == "SL"]),
            "trail_sl_exits": len([t for t in all_trades if t["exit_reason"] == "TRAIL_SL"]),
            "timeout_exits": len([t for t in all_trades if t["exit_reason"] == "TIMEOUT"]),
            "total_fees_usd": round(sum(t["position_size"] * fee_pct / 100 for t in all_trades), 2),
            "avg_duration_hours": round(np.mean([t.get("duration_hours", 0) for t in all_trades]), 1) if all_trades else 0,
            "avg_duration_tp_hours": round(np.mean([t.get("duration_hours", 0) for t in all_trades if t["exit_reason"] == "TP"]), 1) if [t for t in all_trades if t["exit_reason"] == "TP"] else 0,
            "avg_duration_sl_hours": round(np.mean([t.get("duration_hours", 0) for t in all_trades if t["exit_reason"] in ["SL", "TRAIL_SL"]]), 1) if [t for t in all_trades if t["exit_reason"] in ["SL", "TRAIL_SL"]] else 0,
            "avg_duration_long_hours": round(np.mean([t.get("duration_hours", 0) for t in long_trades]), 1) if long_trades else 0,
            "avg_duration_short_hours": round(np.mean([t.get("duration_hours", 0) for t in short_trades]), 1) if short_trades else 0,
            **risk_metrics,
        },
        "monthly": monthly_sorted,
        "rolling_returns": rolling_returns,
        "by_symbol": symbol_stats,
        "by_strategy": trade_breakdowns["by_strategy"],
        "by_regime": trade_breakdowns["by_regime"],
        "by_strategy_direction": by_strategy_direction,
        "by_exit_reason": trade_breakdowns["by_exit_reason"],
        "by_entry_hour_utc": trade_breakdowns["by_entry_hour_utc"],
        "by_entry_weekday_utc": trade_breakdowns["by_entry_weekday_utc"],
        "by_score_band": score_bands,
        "rejection_counts": dict(sorted(rejection_counts.items(), key=lambda x: -x[1])) if rejection_counts else {},
        "equity_curve": equity_curve,
        "trades": all_trades,
    }

    step4_time = time.time() - step4_start
    log.info(f"STEP 4 (stats aggregation) completed in {step4_time:.2f}s")

    # Print overall timing summary
    total_time = step1_time + step2_time + step3_time + step4_time
    log.info(f"\n{'═'*70}")
    log.info(f"  BACKTEST TIMING SUMMARY")
    log.info(f"{'═'*70}")
    log.info(f"  Step 1 (Fetch + validate):     {step1_time:>8.2f}s ({step1_time/total_time*100:>5.1f}%)")
    log.info(f"  Step 2 (Main loop):            {step2_time:>8.2f}s ({step2_time/total_time*100:>5.1f}%)")
    log.info(f"  Step 3 (Force-close):          {step3_time:>8.2f}s ({step3_time/total_time*100:>5.1f}%)")
    log.info(f"  Step 4 (Stats):                {step4_time:>8.2f}s ({step4_time/total_time*100:>5.1f}%)")
    log.info(f"  {'-'*70}")
    log.info(f"  TOTAL TIME:                    {total_time:>8.2f}s")
    log.info(f"{'═'*70}\n")

    return results


def print_summary(results: dict):
    """Print a clean console summary."""
    s = results["summary"]
    c = results["config"]
    by_symbol = results.get("by_symbol", {})
    by_regime = results.get("by_regime", {})
    by_strategy_direction = results.get("by_strategy_direction", {})
    by_exit_reason = results.get("by_exit_reason", {})
    by_score_band = results.get("by_score_band", {})
    by_entry_hour = results.get("by_entry_hour_utc", {})
    by_entry_weekday = results.get("by_entry_weekday_utc", {})
    rejection_counts = results.get("rejection_counts", {})

    def fmt_pf(value) -> str:
        if value == float("inf"):
            return "INF"
        return f"{value:.2f}"

    print("\n" + "═" * 62)
    print(f"  BACKTEST RESULTS — Multi-Regime Scanner (ATR TP/SL)")
    print(f"  {c['months']} months | {', '.join(sym.replace('USDT','') for sym in c['symbols'])}")
    print(f"  TP/SL: ATR-based, strategy-dependent R:R")
    print(f"  Risk: {c['risk_pct']}%/trade | Trend: {c.get('trend_threshold', c['threshold_entry'])}+ | Breakout: {c.get('breakout_threshold', c['threshold_entry'])}+")
    print(f"  Fees: {c['fee_pct']}% RT | Slippage: {c.get('slippage_pct', 0)}% | Trailing: {'ON' if c['trailing_stop'] else 'OFF'}")
    print(f"  Max Pos: {c.get('max_positions', 'N/A')} | Cooldown: {c.get('cooldown_candles', 24)}h | Entry: {'close' if not c.get('use_next_open', True) else 'next open'}")
    print("═" * 62)

    print(f"\n  Starting Account:   ${c['starting_account']:>10,.2f}")
    print(f"  Final Equity:       ${s['final_equity']:>10,.2f}")
    print(f"  Total P&L:          ${s['total_pnl_usd']:>10,.2f}  ({s['total_return_pct']:+.1f}%)")
    print(f"  Max Drawdown:       ${s['max_drawdown_usd']:>10,.2f}  ({s['max_drawdown_pct']:.1f}%)")
    print(f"  Total Fees Paid:    ${s['total_fees_usd']:>10,.2f}")

    print(f"\n  {'─'*50}")
    print(f"  Total Trades:       {s['total_trades']}")
    print(f"  Win Rate:           {s['win_rate']}%  ({s['wins']}W / {s['losses']}L)")
    print(f"  Profit Factor:      {fmt_pf(s['profit_factor'])}")
    print(f"  Avg EV/trade:       {s['avg_ev_per_trade_pct']:+.2f}%  (${s['avg_ev_per_trade_usd']:+.2f})")
    print(f"  Avg Win / Loss:     {s['avg_win_pct']:+.2f}% / {s['avg_loss_pct']:.2f}%")
    print(f"  Avg Win / Loss $:   ${s['avg_win_usd']:+.2f} / ${-s['avg_loss_usd']:.2f}")
    print(f"  Payoff Ratio:       {s['payoff_ratio']:.2f}")
    print(f"  Avg R / Win R / L:  {s['avg_r_multiple']:+.2f} / {s['avg_win_r']:+.2f} / {s['avg_loss_r']:.2f}")
    print(f"  Avg MFE / MAE:      {s['avg_mfe_pct']:.2f}% / {s['avg_mae_pct']:.2f}%")
    print(f"  Best Trade:         {s['best_trade_pct']:+.2f}%")
    print(f"  Worst Trade:        {s['worst_trade_pct']:.2f}%")
    print(f"  Win / Loss Streak:  {s['max_consecutive_wins']} / {s['max_consecutive_losses']}")

    print(f"\n  Exit Reasons:       TP: {s['tp_exits']} | SL: {s['sl_exits']} | Trail SL: {s['trail_sl_exits']} | Timeout: {s['timeout_exits']}")
    print(f"  Direction:          LONG: {s['long_trades']} ({s['long_win_rate']}% WR) | SHORT: {s['short_trades']} ({s['short_win_rate']}% WR)")
    print(f"  Trades/Month:       {s['avg_trades_per_month']}")

    print(f"\n  {'─'*50}")
    print(f"  Risk Diagnostics")
    print(f"  {'─'*50}")
    print(f"  Annualized Return:  {s['annualized_return_pct']:+.2f}%")
    print(f"  Positive Months:    {s['positive_months']} / {s['months_tested']} ({s['consistency_pct']:.1f}%)")
    print(f"  Avg / Best / Worst: {s['avg_monthly_return_pct']:+.2f}% / {s['best_month_return_pct']:+.2f}% / {s['worst_month_return_pct']:+.2f}%")
    print(f"  Vol / Downside Vol: {s['monthly_volatility_pct']:.2f}% / {s['downside_volatility_pct']:.2f}%")
    print(f"  Sharpe / Sortino:   {s['sharpe_ratio']:.2f} / {s['sortino_ratio']:.2f}")
    print(f"  Calmar / Recovery:  {s['calmar_ratio']:.2f} / {s['recovery_factor']:.2f}")

    # Position duration statistics
    if 'avg_duration_hours' in s:
        print(f"\n  {'─'*50}")
        print(f"  Position Duration (in hours)")
        print(f"  {'─'*50}")
        print(f"  Avg Duration:       {s['avg_duration_hours']:.1f}h")
        print(f"  TP Avg Duration:    {s['avg_duration_tp_hours']:.1f}h ({s['tp_exits']} exits)")
        print(f"  SL Avg Duration:    {s['avg_duration_sl_hours']:.1f}h ({s['sl_exits']} exits)")
        print(f"  LONG Avg Duration:  {s['avg_duration_long_hours']:.1f}h")
        print(f"  SHORT Avg Duration: {s['avg_duration_short_hours']:.1f}h")

    # Monthly breakdown
    print(f"\n  {'─'*50}")
    print(f"  {'Month':<10} {'Trades':>6} {'WR':>6} {'P&L':>10} {'Return%':>8}")
    print(f"  {'─'*50}")
    for m in results["monthly"]:
        print(f"  {m['month']:<10} {m['trades']:>6} {m['win_rate']:>5.0f}% ${m['pnl_usd']:>9,.2f} {m['monthly_return_pct']:>+7.2f}%")

    # Rolling returns
    if "rolling_returns" in results:
        print(f"\n  {'─'*50}")
        print(f"  Rolling Returns Summary")
        print(f"  {'─'*50}")
        rr = results["rolling_returns"]

        def format_rolling_row(period_label, period_key):
            if period_key in rr and rr[period_key]["months_available"] > 0:
                data = rr[period_key]
                print(f"  {period_label:<12} ${data['pnl_usd']:>9,.2f}  ({data['return_pct']:>+6.2f}%)  [{data['months_available']}/{data['months_requested']} mo]")
            else:
                print(f"  {period_label:<12} N/A")

        format_rolling_row("1-Month:", "1m")
        format_rolling_row("3-Month:", "3m")
        format_rolling_row("6-Month:", "6m")
        format_rolling_row("12-Month:", "12m")

    if by_score_band:
        print(f"\n  {'─'*50}")
        print(f"  {'Score Band':<12} {'Trades':>6} {'WR':>6} {'Avg%':>8} {'P&L':>10}")
        print(f"  {'─'*50}")
        for band, stats in by_score_band.items():
            print(f"  {band:<12} {stats['trades']:>6} {stats['win_rate']:>5.0f}% {stats['avg_pnl_pct']:>+7.2f}% ${stats['pnl_usd']:>9,.2f}")

    if by_strategy_direction:
        print(f"\n  {'─'*50}")
        print(f"  {'Setup':<28} {'Trades':>6} {'WR':>6} {'PF':>6} {'Avg%':>8} {'P&L':>10}")
        print(f"  {'─'*50}")
        sorted_setups = sorted(
            by_strategy_direction.items(),
            key=lambda item: item[1]["total_pnl_usd"],
            reverse=True,
        )
        for setup, stats in sorted_setups:
            print(
                f"  {setup:<28} {stats['trades']:>6} {stats['win_rate']:>5.0f}% "
                f"{fmt_pf(stats['profit_factor']):>6} {stats['avg_pnl_pct']:>+7.2f}% ${stats['total_pnl_usd']:>9,.2f}"
            )

    if by_exit_reason:
        print(f"\n  {'─'*50}")
        print(f"  {'Exit':<10} {'Trades':>6} {'WR':>6} {'Avg%':>8} {'AvgHrs':>8} {'P&L':>10}")
        print(f"  {'─'*50}")
        for reason, stats in by_exit_reason.items():
            print(
                f"  {reason:<10} {stats['trades']:>6} {stats['win_rate']:>5.0f}% "
                f"{stats['avg_pnl_pct']:>+7.2f}% {stats['avg_duration_hours']:>7.1f}h ${stats['total_pnl_usd']:>9,.2f}"
            )

    if by_symbol:
        print(f"\n  {'─'*50}")
        print(f"  {'Symbol':<10} {'Trades':>6} {'WR':>6} {'P&L':>10} {'Best':>7} {'Worst':>7}")
        print(f"  {'─'*50}")
        for sym, st in sorted(by_symbol.items(), key=lambda x: x[1]["total_pnl_usd"], reverse=True):
            coin = sym.replace("USDT", "")
            print(f"  {coin:<10} {st['total_trades']:>6} {st['win_rate']:>5.0f}% ${st['total_pnl_usd']:>9,.2f} {st['best_trade']:>+6.1f}% {st['worst_trade']:>6.1f}%")

    if by_regime:
        print(f"\n  {'─'*50}")
        print(f"  {'Regime':<22} {'Trades':>6} {'WR':>6} {'PF':>6} {'P&L':>10}")
        print(f"  {'─'*50}")
        for reg, stats in by_regime.items():
            print(f"  {reg:<22} {stats['trades']:>6} {stats['win_rate']:>5.0f}% {fmt_pf(stats['profit_factor']):>6} ${stats['total_pnl_usd']:>9,.2f}")

    if by_entry_weekday:
        print(f"\n  {'─'*50}")
        print(f"  {'Weekday (UTC)':<14} {'Trades':>6} {'WR':>6} {'Avg%':>8} {'P&L':>10}")
        print(f"  {'─'*50}")
        for weekday, stats in by_entry_weekday.items():
            print(f"  {weekday:<14} {stats['trades']:>6} {stats['win_rate']:>5.0f}% {stats['avg_pnl_pct']:>+7.2f}% ${stats['total_pnl_usd']:>9,.2f}")

    if by_entry_hour:
        hour_rows = [(hour, stats) for hour, stats in by_entry_hour.items() if stats["trades"] >= 5]
        if not hour_rows:
            hour_rows = list(by_entry_hour.items())
        hour_rows = sorted(
            hour_rows,
            key=lambda item: (item[1]["avg_pnl_pct"], item[1]["total_pnl_usd"]),
            reverse=True,
        )[:5]
        print(f"\n  {'─'*50}")
        print(f"  Best Entry Hours UTC (min 5 trades when available)")
        print(f"  {'─'*50}")
        print(f"  {'Hour':<8} {'Trades':>6} {'WR':>6} {'Avg%':>8} {'P&L':>10}")
        for hour, stats in hour_rows:
            print(f"  {hour}:00{'':<3} {stats['trades']:>6} {stats['win_rate']:>5.0f}% {stats['avg_pnl_pct']:>+7.2f}% ${stats['total_pnl_usd']:>9,.2f}")

    if rejection_counts:
        print(f"\n  {'─'*50}")
        print(f"  Top Rejection Reasons")
        print(f"  {'─'*50}")
        for reason, count in list(rejection_counts.items())[:5]:
            print(f"  {count:>6}  {reason}")

    print("\n" + "═" * 62)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest Market Scanner V3")
    parser.add_argument("--months", type=int, default=6, help="Months of history (default: 6)")
    parser.add_argument("--symbols", nargs="+", default=None, help="Symbols to test")
    parser.add_argument("--account", type=float, default=1000.0, help="Starting account USD")
    parser.add_argument("--risk-pct", type=float, default=SCANNER_RISK_PCT, help="Risk per trade (from scanner)")
    parser.add_argument("--fee-pct", type=float, default=0.10, help="Round-trip fee (default: 0.10)")
    parser.add_argument("--trailing-stop", action="store_true", help="Enable trailing stop (default: disabled)")
    parser.add_argument("--reset-monthly", action="store_true", help="Reset balance to starting account at beginning of each month")
    parser.add_argument("--output", type=str, default="backtest-results.json", help="Output JSON file")
    # Strategy-specific thresholds (from scanner)
    parser.add_argument("--trend-threshold", type=float, default=SCANNER_TREND_THRESH, help="Min score for trend_pullback signals (from scanner)")
    parser.add_argument("--breakout-threshold", type=float, default=SCANNER_BREAKOUT_THRESH, help="Min score for breakout signals (from scanner)")
    # Position management (from scanner)
    parser.add_argument("--cooldown", type=int, default=SCANNER_COOLDOWN, help="Cooldown candles between signals per symbol (from scanner)")
    parser.add_argument("--max-positions", type=int, default=SCANNER_MAX_POSITIONS, help="Max concurrent open positions (from scanner)")
    parser.add_argument("--slippage-pct", type=float, default=0.05, help="Adverse slippage %% per side (default: 0.05)")
    parser.add_argument("--fixed-size", type=float, default=0.0, help="Fixed $ per trade (0=use risk%% sizing)")
    parser.add_argument("--partial-tp", action="store_true", default=False, help="Close 50%% at 1R, move SL to breakeven (default: disabled)")
    parser.add_argument("--no-kelly-sizing", action="store_false", dest="kelly_sizing", default=True, help="Disable Kelly sizing, use flat risk%% instead")
    parser.add_argument("--signal-model", choices=["technical", "statistical", "statistical_curated", "statistical_wide_short_rsi28", "hybrid_technical_wide_short_rsi28"], default="technical", help="Signal engine to use inside generate_signal()")
    parser.add_argument("--validated-setups", type=str, default=None, help="Path to validated_setups.json for statistical mode")
    # Circuit breaker
    parser.add_argument("--max-drawdown", type=float, default=25.0, help="Max drawdown %% before circuit breaker (default: 25)")
    parser.add_argument("--recovery-candles", type=int, default=168, help="Candles to pause after circuit breaker (default: 168)")
    args = parser.parse_args()

    symbols = args.symbols or [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    ]

    log.info(f"Starting backtest: {args.months}mo, {len(symbols)} symbols, ${args.account} account")
    log.info(f"TP/SL: ATR-based, strategy-dependent R:R (multi-regime)")
    log.info(f"Thresholds: trend={args.trend_threshold} breakout={args.breakout_threshold}")
    log.info(f"Fees: {args.fee_pct}% RT | Slippage: {args.slippage_pct}% | Trailing Stop: {'ON' if args.trailing_stop else 'OFF'}")
    log.info(f"Max positions: {args.max_positions} | Cooldown: {args.cooldown}h | Entry: next candle open")
    log.info(f"Signal model: {args.signal_model}")
    if args.signal_model == "statistical" and args.validated_setups:
        log.info(f"Validated setups: {args.validated_setups}")
    log.info(f"Circuit breaker: {args.max_drawdown}% DD → pause {args.recovery_candles} candles")

    results = run_backtest(
        symbols=symbols,
        months=args.months,
        account=args.account,
        risk_pct=args.risk_pct,
        threshold_entry=min(args.trend_threshold, args.breakout_threshold),
        fee_pct=args.fee_pct,
        trailing_stop=args.trailing_stop,
        reset_monthly=args.reset_monthly,
        cooldown_candles=args.cooldown,
        trend_threshold=args.trend_threshold,
        breakout_threshold=args.breakout_threshold,
        max_positions=args.max_positions,
        slippage_pct=args.slippage_pct,
        use_next_open=True,
        fixed_size=args.fixed_size,
        max_drawdown_pct=args.max_drawdown,
        recovery_candles=args.recovery_candles,
        partial_tp=args.partial_tp,
        kelly_sizing=args.kelly_sizing,
        signal_model=args.signal_model,
        validated_setups_path=args.validated_setups,
    )

    print_summary(results)

    # Save JSON for dashboard
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.output)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    log.info(f"Results saved to {output_path}")
