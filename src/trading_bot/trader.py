#!/usr/bin/env python3
"""
Live Trader — Binance USDM Futures execution loop.

Wraps the existing scanner signal engine with real order execution.
generate_signal() is the single source of truth — same function used by backtester.

Flow every 5 minutes:
  1. Monitor open positions (check TP/SL fills, handle timeouts)
  2. Check circuit breaker + max positions
  3. Scan each symbol for signals
  4. On signal: size position → place market order → place TP + SL orders

Safety defaults:
  - DRY_RUN=true by default (no real orders until explicitly disabled)
  - BINANCE_TESTNET=true by default (testnet API endpoint)
  - Leverage forced to 1x (no leverage)
  - Max 3 concurrent positions (same as backtester)
  - 48h cooldown per symbol after any trade

Config (env vars or config/trader.json):
  BINANCE_API_KEY       Futures API key (needs trading permissions)
  BINANCE_API_SECRET    Futures API secret
  BINANCE_TESTNET       "true" (default) | "false" for live
  DRY_RUN               "true" (default) | "false" to execute real orders
  SIGNAL_MODEL          ta_score | rule_match | combined | combined_validated_rulebook
                        (legacy aliases still supported: technical | statistical |
                         hybrid_technical_statistical)
  RISK_PER_TRADE_PCT    default 1.5
  MAX_POSITIONS         default 3
  SCAN_INTERVAL_SEC     default 300 (5 minutes)
"""

import os
import sys
import copy
import dataclasses
import json
import time
import atexit
import html
import logging
import requests
import signal
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

# ── Imports from the refactored packages ────────────────────────
from . import logging_setup
from .binance_client import BinanceClient
from .binance_http import fetch_klines_cached, send_telegram
from .regime import classify_current_regime
from .signals import config as _shared
from .signals import (
    generate_signal,
    SYMBOLS,
    SIGNAL_THRESHOLD_TREND,
    VALID_SIGNAL_ENGINES,
    RULEBOOK_PATH,
    CURATED_RULEBOOK_PATH,
    _ENGINE_COMPAT_ALIASES,
)
from .signals.filters import _last_rejection_reason

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────
_TRADER_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "trader.json"
)


def get_app_version() -> str:
    """Return deployed app version for logs/alerts."""
    env_version = os.environ.get("APP_VERSION", "").strip()
    if env_version:
        return env_version

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    try:
        return subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip() or "unknown"
    except Exception:
        return "unknown"


def _load_config() -> dict:
    """Load trader config from file, overridden by env vars."""
    cfg = {}
    try:
        with open(_TRADER_CONFIG_PATH) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        pass

    def _get(key, default):
        env = os.environ.get(key)
        if env is not None:
            return env
        return cfg.get(key, default)

    def _bool(val):
        if isinstance(val, bool):
            return val
        return str(val).lower() not in ("false", "0", "no")

    return {
        "dry_run":           _bool(_get("DRY_RUN", True)),
        "signal_engine":     _ENGINE_COMPAT_ALIASES.get(_get("SIGNAL_MODEL", "ta_score").strip().lower(), _get("SIGNAL_MODEL", "ta_score").strip().lower()),
        "risk_pct":          float(_get("RISK_PER_TRADE_PCT", _shared.RISK_PER_TRADE_PCT)),
        "max_positions":     int(_get("MAX_POSITIONS", _shared.MAX_OPEN_POSITIONS)),
        "scan_interval":     int(_get("SCAN_INTERVAL_SEC", 300)),
        # Live trader fetches 1h candles, so SIGNAL_COOLDOWN_CANDLES translates
        # 1:1 to hours. trader.json's COOLDOWN_HOURS still overrides if set.
        "cooldown_hours":    int(_get("COOLDOWN_HOURS", _shared.SIGNAL_COOLDOWN_CANDLES)),
        "timeout_hours":     int(_get("TIMEOUT_HOURS", 120)),
        "max_drawdown_pct":  float(_get("MAX_DRAWDOWN_PCT", 12.0)),
        "circuit_break_hours": int(_get("CIRCUIT_BREAK_HOURS", 168)),
        "window_size":       int(_get("WINDOW_SIZE", 1000)),
    }


# ─────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────
from logging.handlers import RotatingFileHandler

_LOG_FILE = os.environ.get("TRADING_BOT_LOG", "/tmp/trading-bot.log")
_DEBUG_LOG_FILE = os.environ.get("TRADING_BOT_DEBUG_LOG", "/tmp/trading-bot-debug.log")
os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)

log = logging.getLogger("trading-bot")
log.setLevel(logging.DEBUG)  # Capture everything, filter by handler level
log.propagate = False
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

# Clear any existing handlers (prevents duplicates on module reload)
for handler in log.handlers[:]:
    log.removeHandler(handler)

# INFO handler — monitoring log
_fh = RotatingFileHandler(_LOG_FILE, maxBytes=50 * 1024 * 1024, backupCount=5)
_fh.setLevel(logging.INFO)
_fh.setFormatter(_fmt)
log.addHandler(_fh)

# DEBUG handler — verbose debug log
_dfh = RotatingFileHandler(_DEBUG_LOG_FILE, maxBytes=50 * 1024 * 1024, backupCount=5)
_dfh.setLevel(logging.DEBUG)
_dfh.setFormatter(_fmt)
log.addHandler(_dfh)

if sys.stdout.isatty():
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setLevel(logging.INFO)
    _sh.setFormatter(_fmt)
    log.addHandler(_sh)

# ─────────────────────────────────────────────────────────────────
# STATE  (persisted to disk)
# ─────────────────────────────────────────────────────────────────
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data")
os.makedirs(_DATA_DIR, exist_ok=True)
_STATE_FILE = os.environ.get("TRADER_STATE", os.path.join(_DATA_DIR, "trader-state.json"))

def _new_empty_state() -> dict:
    return {
        "positions": {},        # symbol → position dict
        "last_signal": {},      # symbol → ISO timestamp of last signal
        "peak_equity": None,    # float — for circuit breaker
        "circuit_breaker_until": None,  # ISO timestamp or None
        "trade_log": [],        # closed trade records
    }


_EMPTY_STATE = _new_empty_state()


def load_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            s = json.load(f)
            # Ensure all keys present (forward-compat with older state files)
            for k, v in _new_empty_state().items():
                s.setdefault(k, v)
            return s
    except Exception:
        return _new_empty_state()


def save_state(state: dict):
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _write_position_state(
    state: dict,
    symbol: str,
    direction: str,
    fill_price: float,
    quantity: float,
    score: float,
    strategy: str,
    signal_engine: str,
    price_precision: int,
    tp_price: Optional[float] = None,
    sl_price: Optional[float] = None,
    tp_order_id: Optional[str] = None,
    sl_order_id: Optional[str] = None,
    protection_status: str = "pending",
    selected_source: Optional[str] = None,
    setup_name: Optional[str] = None,
    market_regime: Optional[str] = None,
):
    state["positions"][symbol] = {
        "symbol": symbol,
        "direction": direction,
        "entry_price": fill_price,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "quantity": quantity,
        "position_size_usdt": round(quantity * fill_price, 2),
        "tp_price": None if tp_price is None else round(tp_price, price_precision),
        "sl_price": None if sl_price is None else round(sl_price, price_precision),
        "tp_order_id": tp_order_id,
        "sl_order_id": sl_order_id,
        "score": score,
        "strategy": strategy,
        "signal_engine": signal_engine,
        "protection_status": protection_status,
        "selected_source": selected_source,
        "setup_name": setup_name,
        "market_regime": market_regime,
    }
    save_state(state)


def _signal_audit_context(signal, default_engine: Optional[str] = None) -> dict:
    """Extract a compact audit context for logs, state, and Telegram alerts."""
    hybrid_details = signal.hybrid_details or {}
    selected = hybrid_details.get("selected") or {}

    engine = signal.signal_engine or default_engine or "unknown"
    source = signal.selected_source or selected.get("source")
    if not source:
        if signal.statistical_setup or engine == "rule_match":
            source = "statistical"
        elif engine in {"ta_score", "technical"}:
            source = "technical"
        else:
            source = engine

    setup_name = (
        signal.setup_name
        or signal.statistical_setup
        or (hybrid_details.get("statistical") or {}).get("setup")
        or signal.strategy
        or "unknown"
    )
    market_regime = (
        signal.market_regime
        or signal.regime
        or (signal.details or {}).get("regime")
        or "unknown"
    )

    return {
        "engine": engine,
        "source": source,
        "setup_name": setup_name,
        "market_regime": market_regime,
    }


def _position_audit_context(position: dict) -> dict:
    return {
        "engine": position.get("signal_engine") or "unknown",
        "source": position.get("selected_source") or "unknown",
        "setup_name": position.get("setup_name") or position.get("strategy") or "unknown",
        "market_regime": position.get("market_regime") or "unknown",
        "protection_status": position.get("protection_status") or "unknown",
    }


def _format_telegram_audit_lines(
    *,
    engine: str,
    source: str,
    setup_name: str,
    market_regime: str,
    protection_status: Optional[str] = None,
) -> str:
    """One compact audit line; protection called out only when NOT armed.

    `engine` is accepted for caller compatibility but not shown — it is
    effectively constant in prod and was pure noise in alerts.
    """
    line = (
        f"Setup: <code>{html.escape(str(setup_name))}</code> "
        f"({html.escape(str(market_regime))} · {html.escape(str(source))})"
    )
    if protection_status is not None and str(protection_status) != "armed":
        line += f"\n⚠️ Protection: <code>{html.escape(str(protection_status))}</code>"
    return line


def _format_close_footer(state: dict, client, dry_run: bool) -> str:
    """'Today: +$X | Equity: $Y' — account context for every close alert."""
    today = datetime.now(timezone.utc).date().isoformat()
    today_pnl = sum(
        float(t.get("pnl_usd", 0) or 0)
        for t in state.get("trade_log", [])
        if str(t.get("exit_time", "")).startswith(today)
    )
    footer = f"Today: <b>{today_pnl:+.2f}</b> USDT"
    if not dry_run:
        try:
            breakdown = client.get_balance_breakdown()
            if breakdown is not None:
                wallet, floating = breakdown
                footer += f" | Wallet: <b>${wallet:,.2f}</b> | Floating: {floating:+.2f}"
        except Exception:
            pass  # alert must never fail on a balance call
    return footer


def _bump_rejection_count(rejection_counts: dict[str, int], reason: str) -> None:
    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1


def _format_rejection_summary(rejection_counts: dict[str, int]) -> str:
    ordered = sorted(rejection_counts.items(), key=lambda item: (-item[1], item[0]))
    return " | ".join(f"{reason}={count}" for reason, count in ordered)


def _recover_position_state_from_binance(state: dict, live_position: dict, client: BinanceClient):
    """Import a live Binance position into local state so monitoring can resume after restart."""
    symbol = live_position["symbol"]
    position_amt = float(live_position.get("positionAmt", 0.0))
    quantity = abs(position_amt)
    if quantity == 0:
        return

    direction = "LONG" if position_amt > 0 else "SHORT"
    entry_price = float(live_position.get("entryPrice", 0.0) or 0.0)
    if entry_price <= 0:
        mark_price = float(live_position.get("markPrice", 0.0) or 0.0)
        entry_price = mark_price

    sym_info = client.get_symbol_info(symbol) or {}
    price_precision = int(sym_info.get("price_precision", 4))

    tp_price = None
    sl_price = None
    tp_order_id = None
    sl_order_id = None

    for order in client.get_open_algo_orders(symbol):
        side = str(order.get("side", "")).upper()
        close_side = "SELL" if direction == "LONG" else "BUY"
        if side != close_side:
            continue

        order_type = str(order.get("type") or order.get("origType") or order.get("algoType") or "").upper()
        trigger_price = order.get("triggerPrice") or order.get("stopPrice")
        client_algo_id = order.get("clientAlgoId")
        if trigger_price is None or not client_algo_id:
            continue

        order_ref = BinanceClient._algo_order_ref(str(client_algo_id))
        trigger_price = round(float(trigger_price), price_precision)

        if "TAKE_PROFIT" in order_type:
            tp_price = trigger_price
            tp_order_id = order_ref
        elif order_type == "STOP_MARKET" or "STOP" in order_type:
            sl_price = trigger_price
            sl_order_id = order_ref

    protection_status = "armed" if tp_order_id and sl_order_id else "untracked"

    state["positions"][symbol] = {
        "symbol": symbol,
        "direction": direction,
        "entry_price": round(entry_price, price_precision),
        # Unknown original open time after a crash/restart; use recovery time to avoid false timeout exits.
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "quantity": quantity,
        "position_size_usdt": round(quantity * entry_price, 2),
        "tp_price": tp_price,
        "sl_price": sl_price,
        "tp_order_id": tp_order_id,
        "sl_order_id": sl_order_id,
        "score": None,
        "strategy": "recovered_from_binance",
        "signal_engine": "recovered",
        "protection_status": protection_status,
        "recovered_from_binance": True,
    }


# ─────────────────────────────────────────────────────────────────
# POSITION SIZING  (mirrors backtester logic)
# ─────────────────────────────────────────────────────────────────
from .core.tp_sl import compute_tp_sl
from .core.types import SignalEngine
from .execution.sizing import size_position_notional
from .execution.gating import (
    required_threshold as _gate_required_threshold,
    in_cooldown as _gate_in_cooldown,
)


def size_position(
    equity: float,
    risk_pct: float,
    sl_pct: float,
    entry_price: float,
    qty_precision: int,
    min_qty: float,
    min_notional: float,
    max_positions: int = 3,
) -> Optional[float]:
    """Live wrapper: shared notional formula → quantity, with exchange filters.

    `equity` must be total wallet equity (free + locked + unrealized PnL).
    Returns base-asset quantity, or None if below exchange minimums.
    """
    if sl_pct <= 0 or entry_price <= 0:
        return None

    position_usdt = size_position_notional(
        equity, risk_pct, sl_pct, max_positions=max_positions
    )

    quantity = position_usdt / entry_price
    quantity = round(quantity, qty_precision)

    if quantity < min_qty:
        log.warning(f"Quantity {quantity} below min_qty {min_qty}")
        return None
    if quantity * entry_price < min_notional:
        log.warning(f"Notional {quantity * entry_price:.2f} below min_notional {min_notional}")
        return None

    return quantity


# ─────────────────────────────────────────────────────────────────
# THRESHOLD  (delegates to execution/gating.py — shared with backtester)
# ─────────────────────────────────────────────────────────────────
def _required_threshold(signal_engine: SignalEngine, strategy: str) -> Optional[float]:
    """Return minimum score threshold, or None to bypass (rule match is the gate)."""
    return _gate_required_threshold(
        signal_engine, strategy,
        trend_threshold=SIGNAL_THRESHOLD_TREND,
    )


# ─────────────────────────────────────────────────────────────────
# COOLDOWN HELPERS
# ─────────────────────────────────────────────────────────────────
def _in_cooldown(state: dict, symbol: str, cooldown_hours: int) -> bool:
    """Live cooldown check — parses the stored timestamp, delegates the math."""
    last_str = state["last_signal"].get(symbol)
    if not last_str:
        return False
    last_dt = datetime.fromisoformat(last_str)
    elapsed_seconds = (datetime.now(timezone.utc) - last_dt).total_seconds()
    return _gate_in_cooldown(elapsed_seconds, cooldown_hours * 3600)


def _set_cooldown(state: dict, symbol: str):
    state["last_signal"][symbol] = datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────────────────────────
def _circuit_breaker_active(state: dict) -> bool:
    until_str = state.get("circuit_breaker_until")
    if not until_str:
        return False
    until_dt = datetime.fromisoformat(until_str)
    return datetime.now(timezone.utc) < until_dt


def _check_circuit_breaker(state: dict, current_equity: float, cfg: dict):
    peak = state.get("peak_equity") or current_equity
    if current_equity > peak:
        state["peak_equity"] = current_equity
        return

    dd_pct = (peak - current_equity) / peak * 100 if peak > 0 else 0
    if dd_pct >= cfg["max_drawdown_pct"]:
        resume = datetime.now(timezone.utc) + timedelta(hours=cfg["circuit_break_hours"])
        state["circuit_breaker_until"] = resume.isoformat()
        log.warning(f"CIRCUIT BREAKER: {dd_pct:.1f}% drawdown. Pausing until {resume.isoformat()}")
        send_telegram(
            f"⛔ <b>CIRCUIT BREAKER TRIGGERED</b>\n"
            f"Drawdown: {dd_pct:.1f}% (peak ${peak:.0f} → current ${current_equity:.0f})\n"
            f"Trading paused until {resume.strftime('%Y-%m-%d %H:%M UTC')}"
        )


# ─────────────────────────────────────────────────────────────────
# POSITION MONITORING  (called every cycle before scanning)
# ─────────────────────────────────────────────────────────────────
def monitor_positions(state: dict, client: BinanceClient, cfg: dict, dry_run: bool):
    """
    Check all open positions for TP/SL fills or timeout.
    Closes out any filled/expired positions and updates state.
    """
    closed = []

    for symbol, pos in list(state["positions"].items()):
        tp_id = pos.get("tp_order_id")
        sl_id = pos.get("sl_order_id")
        entry_time = datetime.fromisoformat(pos["entry_time"])
        age_hours = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600

        exit_reason = None
        exit_price = None

        if not dry_run:
            # Check TP fill
            if tp_id:
                tp_status = client.get_order_status(symbol, tp_id)
                if tp_status == "FILLED":
                    exit_reason = "TP"
                    exit_price = client.get_order_fill_price(symbol, tp_id)

            # Check SL fill
            if sl_id and exit_reason is None:
                sl_status = client.get_order_status(symbol, sl_id)
                if sl_status == "FILLED":
                    exit_reason = "SL"
                    exit_price = client.get_order_fill_price(symbol, sl_id)

        # Timeout (180h = 7.5 days)
        if exit_reason is None and age_hours >= cfg["timeout_hours"]:
            if not dry_run:
                close_side = "SELL" if pos["direction"] == "LONG" else "BUY"
                result = client.place_market_close(symbol, close_side, pos["quantity"])
                if result:
                    exit_reason = "TIMEOUT"
                    exit_price = result["filled_price"]
                    client.cancel_all_orders(symbol)
                else:
                    # Close failed — keep position in state, retry next cycle
                    log.error(f"  {symbol}: TIMEOUT close failed, will retry next cycle")
                    send_telegram(f"⚠️ {symbol} TIMEOUT close FAILED — retrying next cycle")
            else:
                exit_reason = "TIMEOUT"
                exit_price = pos["entry_price"]  # dry-run placeholder

        if exit_reason is not None:
            # Cancel the leg that didn't fill
            if not dry_run:
                if exit_reason in ("TP", "TIMEOUT") and sl_id:
                    client.cancel_order(symbol, sl_id)
                elif exit_reason == "SL" and tp_id:
                    client.cancel_order(symbol, tp_id)

            # Record closed trade
            if exit_price:
                entry = pos["entry_price"]
                direction = pos["direction"]
                audit = _position_audit_context(pos)
                if direction == "LONG":
                    pnl_pct = (exit_price - entry) / entry * 100
                else:
                    pnl_pct = (entry - exit_price) / entry * 100
                pnl_usd = pos["position_size_usdt"] * (pnl_pct / 100)

                trade_record = {
                    "symbol": symbol,
                    "direction": direction,
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "entry_time": pos["entry_time"],
                    "exit_time": datetime.now(timezone.utc).isoformat(),
                    "pnl_pct": round(pnl_pct, 3),
                    "pnl_usd": round(pnl_usd, 2),
                    "score": pos.get("score"),
                    "strategy": pos.get("strategy"),
                    "setup_name": pos.get("setup_name"),
                    "market_regime": pos.get("market_regime"),
                    "selected_source": pos.get("selected_source"),
                }
                state["trade_log"].append(trade_record)

                emoji = "✅" if pnl_pct > 0 else "❌"
                dir_emoji = "🟢" if direction == "LONG" else "🔴"
                log.info(f"  {emoji} CLOSED {symbol} {exit_reason}: {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
                send_telegram(
                    f"{emoji} <b>{exit_reason}</b> {dir_emoji} {symbol.replace('USDT','')} "
                    f"{'LONG' if direction == 'LONG' else 'SHORT'}\n"
                    f"Entry: <code>${entry:,.4f}</code> → Exit: <code>${exit_price:,.4f}</code>\n"
                    f"PnL: <b>{pnl_pct:+.2f}%</b> (${pnl_usd:+.2f}) | Held: {age_hours:.1f}h\n"
                    f"{_format_close_footer(state, client, dry_run)}\n"
                    f"{_format_telegram_audit_lines(**audit)}"
                )

            closed.append(symbol)

    for symbol in closed:
        del state["positions"][symbol]

    if closed:
        save_state(state)


# ─────────────────────────────────────────────────────────────────
# ENTRY EXECUTION
# ─────────────────────────────────────────────────────────────────
def execute_entry(
    signal: dict,
    state: dict,
    client: BinanceClient,
    cfg: dict,
    dry_run: bool,
    equity: float,
) -> bool:
    """
    Size and execute a trade entry. Returns True if position was opened.

    On real Binance:
      1. Set leverage to 1x
      2. Place market order (entry)
      3. Re-anchor TP/SL from actual fill price
      4. Place TP + SL orders
      5. If either fails → emergency market close

    On dry run:
      Simulates fill at signal entry_price, logs as if real.
    """
    symbol    = signal.symbol
    direction = signal.direction
    entry_p   = signal.entry_price
    tp_p      = signal.tp
    sl_p      = signal.sl
    sl_pct    = abs(signal.sl_pct)
    tp_pct    = abs(signal.tp_pct)
    strategy  = signal.strategy
    score     = signal.score
    audit = _signal_audit_context(signal, cfg.get("signal_engine"))

    entry_side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"

    if not dry_run:
        sym_info = client.get_symbol_info(symbol)
        if not sym_info:
            log.error(f"  {symbol}: Could not get symbol info, skipping")
            return False

        qty_prec   = sym_info["qty_precision"]
        price_prec = sym_info["price_precision"]
        min_qty    = sym_info["min_qty"]
        min_notional = sym_info["min_notional"]
    else:
        qty_prec, price_prec = 3, 2
        min_qty, min_notional = 0.001, 5.0

    quantity = size_position(
        equity, cfg["risk_pct"], sl_pct, entry_p, qty_prec, min_qty, min_notional,
        max_positions=cfg["max_positions"]
    )
    if quantity is None:
        log.warning(f"  {symbol}: Position size below minimum, skipping")
        return False

    position_usdt = quantity * entry_p
    log.info(
        f"  {symbol}: {direction} | entry=${entry_p:.4f} | qty={quantity} | "
        f"notional=${position_usdt:.2f} | TP={tp_pct:.2f}% SL={sl_pct:.2f}%"
    )

    if dry_run:
        fill_price = entry_p
        tp_order_id = "DRY_TP"
        sl_order_id = "DRY_SL"
        log.info(f"  [DRY RUN] Simulated entry: {symbol} {direction} @ {fill_price:.4f}")
    else:
        # 1. Set leverage to 1x (no leverage)
        client.set_leverage(symbol, 1)

        # 2. Market entry
        fill = client.place_market_order(symbol, entry_side, quantity)
        if not fill:
            log.error(f"  {symbol}: Market order failed")
            return False

        fill_price = fill["filled_price"] or entry_p
        log.info(f"  {symbol}: Filled @ {fill_price:.4f} (ordered @ {entry_p:.4f})")

        # Persist immediately after market fill so a restart can reconcile a live
        # position even if the process dies before TP/SL placement completes.
        _write_position_state(
            state,
            symbol=symbol,
            direction=direction,
            fill_price=fill_price,
            quantity=quantity,
            score=score,
            strategy=strategy,
            signal_engine=cfg["signal_engine"],
            price_precision=price_prec,
            protection_status="pending",
            selected_source=audit["source"],
            setup_name=audit["setup_name"],
            market_regime=audit["market_regime"],
        )

        # 3. Compute TP/SL fresh at the actual fill price (shared with backtester).
        atr_val = float(signal.atr or 0.0)
        sl_atr_mult = float(signal.sl_atr_mult or 1.5)
        rr_ratio = float(signal.rr_ratio or 2.0)
        tp_p, sl_p = compute_tp_sl(
            direction, fill_price, atr_val, sl_atr_mult, rr_ratio,
        )

        # 4. Place TP order
        tp_order_id = client.place_tp_order(symbol, close_side, tp_p, price_prec)
        if not tp_order_id:
            log.error(f"  {symbol}: TP order failed — emergency closing position")
            result = client.place_market_close(symbol, close_side, quantity)
            if not result:
                log.error(f"  {symbol}: EMERGENCY CLOSE ALSO FAILED — position open without TP/SL!")
                send_telegram(f"🚨 {symbol} EMERGENCY CLOSE FAILED — manual intervention needed!")
            state["positions"].pop(symbol, None)
            _set_cooldown(state, symbol)  # prevent re-entry on next scan cycle
            save_state(state)
            return False

        # 5. Place SL order
        sl_order_id = client.place_sl_order(symbol, close_side, sl_p, price_prec)
        if not sl_order_id:
            log.error(f"  {symbol}: SL order failed — emergency closing position")
            client.cancel_order(symbol, tp_order_id)
            result = client.place_market_close(symbol, close_side, quantity)
            if not result:
                log.error(f"  {symbol}: EMERGENCY CLOSE ALSO FAILED — position has TP but no SL!")
                send_telegram(f"🚨 {symbol} EMERGENCY CLOSE FAILED — has TP but no SL! Manual intervention needed!")
            state["positions"].pop(symbol, None)
            _set_cooldown(state, symbol)  # prevent re-entry on next scan cycle
            save_state(state)
            return False

    # Save fully protected position to state
    _write_position_state(
        state,
        symbol=symbol,
        direction=direction,
        fill_price=fill_price,
        quantity=quantity,
        score=score,
        strategy=strategy,
        signal_engine=cfg["signal_engine"],
        price_precision=price_prec if not dry_run else 4,
        tp_price=tp_p,
        sl_price=sl_p,
        tp_order_id=tp_order_id,
        sl_order_id=sl_order_id,
        protection_status="armed",
        selected_source=audit["source"],
        setup_name=audit["setup_name"],
        market_regime=audit["market_regime"],
    )
    _set_cooldown(state, symbol)
    save_state(state)

    dir_emoji = "🟢" if direction == "LONG" else "🔴"
    dry_tag = " [DRY RUN]" if dry_run else ""
    risk_usd = position_usdt * (sl_pct / 100.0)
    rr = (tp_pct / sl_pct) if sl_pct > 0 else 0.0
    send_telegram(
        f"{dir_emoji} <b>NEW TRADE{dry_tag}</b> {symbol.replace('USDT','')} "
        f"{'LONG' if direction == 'LONG' else 'SHORT'}\n"
        f"Entry: <code>${fill_price:,.4f}</code>\n"
        f"TP: <code>${tp_p:,.4f}</code> (+{tp_pct:.2f}%) | "
        f"SL: <code>${sl_p:,.4f}</code> (-{sl_pct:.2f}%) | R:R {rr:.1f}\n"
        f"Size: ${position_usdt:.0f} ({quantity} {symbol.replace('USDT','')}) | "
        f"Risk: ${risk_usd:.2f}\n"
        f"{_format_telegram_audit_lines(**audit, protection_status='armed')}"
    )

    return True


# ─────────────────────────────────────────────────────────────────
# STARTUP RECONCILIATION
# ─────────────────────────────────────────────────────────────────
def _record_reconciled_close(state: dict, client: BinanceClient, symbol: str) -> None:
    """Reconstruct a closed trade from Binance fills and append to trade_log.

    Called when reconcile finds a position in local state that Binance no longer has —
    the position was closed (TP/SL hit, manual, or liquidation) between monitor cycles.
    Without this, trade_log stays empty and we lose all PnL visibility on silent closes.
    """
    pos = state["positions"].get(symbol)
    if not pos:
        return

    direction = pos["direction"]
    audit = _position_audit_context(pos)
    entry_price = float(pos["entry_price"])
    quantity = float(pos["quantity"])
    position_size = float(pos.get("position_size_usdt") or (entry_price * quantity))

    try:
        entry_dt = datetime.fromisoformat(pos["entry_time"])
        start_ms = int(entry_dt.timestamp() * 1000)
    except (KeyError, ValueError):
        start_ms = None

    fills = client.get_user_trades(symbol, start_ms=start_ms)
    close_side = "SELL" if direction == "LONG" else "BUY"
    # Only fills on the closing side contribute to exit price / realized PnL for this trade.
    closing_fills = [f for f in fills if str(f.get("side", "")).upper() == close_side]

    exit_price = None
    pnl_usd = None
    exit_time_ms = None
    if closing_fills:
        total_qty = sum(float(f.get("qty", 0) or 0) for f in closing_fills)
        total_notional = sum(float(f.get("qty", 0) or 0) * float(f.get("price", 0) or 0) for f in closing_fills)
        total_realized = sum(float(f.get("realizedPnl", 0) or 0) for f in closing_fills)
        if total_qty > 0:
            exit_price = total_notional / total_qty
            pnl_usd = total_realized
            exit_time_ms = max(int(f.get("time", 0) or 0) for f in closing_fills)

    # Determine exit reason from TP/SL order status
    exit_reason = "UNKNOWN"
    tp_id = pos.get("tp_order_id")
    sl_id = pos.get("sl_order_id")
    if tp_id:
        if client.get_order_status(symbol, tp_id) == "FILLED":
            exit_reason = "TP"
    if exit_reason == "UNKNOWN" and sl_id:
        if client.get_order_status(symbol, sl_id) == "FILLED":
            exit_reason = "SL"
    # Fallback: algo order records expire on Binance — infer from price proximity
    if exit_reason == "UNKNOWN" and exit_price is not None:
        tp_price = pos.get("tp_price")
        sl_price = pos.get("sl_price")
        if tp_price and sl_price:
            dist_tp = abs(exit_price - float(tp_price))
            dist_sl = abs(exit_price - float(sl_price))
            exit_reason = "TP" if dist_tp < dist_sl else "SL"

    if exit_price is not None and pnl_usd is not None:
        pnl_pct = (pnl_usd / position_size * 100) if position_size > 0 else 0.0
        exit_time_iso = (
            datetime.fromtimestamp(exit_time_ms / 1000, tz=timezone.utc).isoformat()
            if exit_time_ms
            else datetime.now(timezone.utc).isoformat()
        )
        trade_record = {
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": round(exit_price, 8),
            "exit_reason": exit_reason,
            "entry_time": pos["entry_time"],
            "exit_time": exit_time_iso,
            "pnl_pct": round(pnl_pct, 3),
            "pnl_usd": round(pnl_usd, 2),
            "score": pos.get("score"),
            "strategy": pos.get("strategy"),
            "setup_name": pos.get("setup_name"),
            "market_regime": pos.get("market_regime"),
            "selected_source": pos.get("selected_source"),
            "reconciled": True,
        }
        state["trade_log"].append(trade_record)

        emoji = "✅" if pnl_usd > 0 else "❌"
        dir_emoji = "🟢" if direction == "LONG" else "🔴"
        log.info(
            f"  {emoji} RECONCILED {symbol} {exit_reason}: {pnl_pct:+.2f}% (${pnl_usd:+.2f})"
        )
        send_telegram(
            f"{emoji} <b>{exit_reason}</b> {dir_emoji} {symbol.replace('USDT','')} "
            f"{direction} <i>(reconciled)</i>\n"
            f"Entry: <code>${entry_price:,.4f}</code> → Exit: <code>${exit_price:,.4f}</code>\n"
            f"PnL: <b>{pnl_pct:+.2f}%</b> (${pnl_usd:+.2f})\n"
            f"{_format_close_footer(state, client, dry_run=False)}\n"
            f"{_format_telegram_audit_lines(**audit)}"
        )
    else:
        log.warning(
            f"  ⚠️ {symbol}: closed on Binance but no fill history recoverable — "
            f"no PnL recorded (fills_found={len(closing_fills)})"
        )
        send_telegram(
            f"⚠️ <b>Silent close</b> {symbol.replace('USDT','')} {direction}\n"
            f"Position gone on Binance but fill history unrecoverable — verify manually."
        )


def reconcile_with_binance(state: dict, client: BinanceClient):
    """
    On startup and periodically: compare state positions vs Binance actual positions.
    Record PnL for stale local positions, remove them, and import unknown live positions.
    """
    live_position_rows = client.get_open_positions()
    live_positions = {p["symbol"] for p in live_position_rows}
    state_symbols = set(state["positions"].keys())

    stale = state_symbols - live_positions
    unknown = live_positions - state_symbols

    if stale:
        log.warning(
            f"State has positions Binance doesn't: {stale}. "
            f"These may have been closed externally. Recording PnL and removing from state."
        )
        for sym in stale:
            _record_reconciled_close(state, client, sym)
            # Clean up any orphan TP/SL algo orders that didn't fill.
            try:
                client.cancel_all_orders(sym)
            except Exception as exc:
                log.warning(f"  {sym}: cancel_all_orders after reconcile failed: {exc}")
            del state["positions"][sym]
        save_state(state)

    if unknown:
        imported = []
        unprotected = []
        live_by_symbol = {p["symbol"]: p for p in live_position_rows}
        for sym in sorted(unknown):
            live_position = live_by_symbol.get(sym)
            if not live_position:
                continue
            _recover_position_state_from_binance(state, live_position, client)
            imported.append(sym)
            if state["positions"][sym].get("protection_status") != "armed":
                unprotected.append(sym)

        save_state(state)
        log.warning(f"Recovered Binance positions into state: {imported}")
        details = ""
        if unprotected:
            details = f"\nProtection orders not fully recovered: {', '.join(unprotected)}"
        send_telegram(
            f"⚠️ <b>Position mismatch on startup</b>\n"
            f"Recovered open Binance positions into bot state: {', '.join(imported)}"
            f"{details}"
        )


# ─────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────
_PID_FILE = os.path.join(_DATA_DIR, "trader.pid")


def _acquire_pid_lock():
    """Prevent duplicate trader instances. Returns True if lock acquired."""
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE) as f:
                old_pid = int(f.read().strip())
            # Check if process is still running
            os.kill(old_pid, 0)
            # Process exists — another instance is running
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            # PID file stale — process no longer running
            pass
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def _release_pid_lock():
    try:
        os.remove(_PID_FILE)
    except OSError:
        pass


def _handle_exit_signal(signum, frame):
    _release_pid_lock()
    raise SystemExit(0)


def _build_startup_banner(cfg: dict, signal_engine: str, mode_tag: str, version: str) -> list[str]:
    """Build startup banner lines for consistent logging and tests."""
    return [
        "=" * 60,
        f"Trader {mode_tag}starting (PID: {os.getpid()})",
        f"Version:        {version}",
        f"Signal engine:  {signal_engine}",
        f"Risk per trade: {cfg['risk_pct']}%",
        f"Max positions:  {cfg['max_positions']}",
        f"Cooldown:       {cfg['cooldown_hours']}h",
        f"Symbols:        {', '.join(SYMBOLS)}",
        "=" * 60,
    ]


def main():
    # Default to LIVE mode (unless explicitly overridden)
    if "BINANCE_TESTNET" not in os.environ:
        os.environ["BINANCE_TESTNET"] = "false"

    if not _acquire_pid_lock():
        print(f"Another trader instance is already running (PID file: {_PID_FILE})")
        sys.exit(1)

    atexit.register(_release_pid_lock)
    signal.signal(signal.SIGTERM, _handle_exit_signal)
    signal.signal(signal.SIGINT, _handle_exit_signal)

    logging_setup.configure_logging()

    cfg = _load_config()
    dry_run = cfg["dry_run"]

    signal_engine = cfg["signal_engine"]
    if signal_engine not in VALID_SIGNAL_ENGINES:
        signal_engine = "ta_score"
    app_version = get_app_version()

    validated_path = RULEBOOK_PATH

    mode_tag = "[DRY RUN] " if dry_run else "[LIVE] "
    for line in _build_startup_banner(cfg, signal_engine, mode_tag, app_version):
        log.info(line)

    # Init Binance client
    try:
        client = BinanceClient()
    except ValueError as e:
        log.error(f"Failed to init Binance client: {e}")
        sys.exit(1)

    if not dry_run:
        client.ensure_one_way_mode()

    state = load_state()

    # Reconcile state vs Binance on startup
    if not dry_run:
        reconcile_with_binance(state, client)

    startup_account = ""
    if not dry_run:
        try:
            breakdown = client.get_balance_breakdown()
            if breakdown is not None:
                wallet, floating = breakdown
                open_syms = [s.replace("USDT", "") for s in state["positions"]]
                open_str = f"{len(open_syms)} ({', '.join(open_syms)})" if open_syms else "0"
                startup_account = (
                    f"Wallet: <b>${wallet:,.2f}</b> | Floating: {floating:+.2f} | Open: {open_str}\n"
                )
        except Exception:
            pass
    # Testnet called out only when it's ON — silence is the normal (live) state.
    testnet_warning = "⚠️ <b>TESTNET mode</b>\n" if client.testnet else ""
    send_telegram(
        f"🤖 <b>Trader Online {mode_tag}</b>\n"
        f"{testnet_warning}"
        f"<code>{app_version}</code> · {signal_engine} · "
        f"{len(SYMBOLS)} symbols\n"
        f"Risk {cfg['risk_pct']}% / trade · Max {cfg['max_positions']} pos · "
        f"Cooldown {cfg['cooldown_hours']}h · Timeout {cfg['timeout_hours']}h\n"
        f"{startup_account}"
    )

    cycle_count = 0

    while True:
        cycle_start = time.time()
        now = datetime.now(timezone.utc)
        cycle_count += 1
        log.info(f"\n{'─'*50}")
        log.info(f"Cycle @ {now.strftime('%Y-%m-%d %H:%M:%S UTC')} | {mode_tag}")

        # ── 1. Monitor open positions ──────────────────────────────
        if state["positions"]:
            log.info(f"Monitoring {len(state['positions'])} open position(s)...")
            monitor_positions(state, client, cfg, dry_run)

        # Periodic reconcile (every 12 cycles = ~1h) to self-heal stale positions
        if not dry_run and cycle_count % 12 == 0:
            log.info("Running periodic reconcile with Binance...")
            reconcile_with_binance(state, client)

        # ── 2. Get current balance ─────────────────────────────────
        if not dry_run:
            balance = client.get_usdt_balance()
            equity = client.get_usdt_equity()  # Total equity for circuit breaker
            if balance is None:
                log.error("Failed to get balance from Binance, skipping entry scan")
                time.sleep(cfg["scan_interval"])
                continue
            if equity is None:
                equity = balance  # Fallback to available balance
        else:
            balance = float(os.environ.get("DRY_RUN_BALANCE", "1000"))
            equity = balance

        log.info(f"Balance: ${balance:.2f} USDT (equity: ${equity:.2f})")

        # ── 3. Circuit breaker (uses total equity, not available balance) ──
        _check_circuit_breaker(state, equity, cfg)
        if _circuit_breaker_active(state):
            until = state["circuit_breaker_until"]
            log.warning(f"Circuit breaker active until {until}, skipping entries")
            elapsed = time.time() - cycle_start
            time.sleep(max(0, cfg["scan_interval"] - elapsed))
            continue

        # ── 4. Scan for entries ────────────────────────────────────
        open_count = len(state["positions"])
        if open_count >= cfg["max_positions"]:
            log.info(f"At max positions ({open_count}/{cfg['max_positions']}), skipping scan")
        else:
            log.info(f"Scanning {len(SYMBOLS)} symbols ({open_count}/{cfg['max_positions']} positions open)...")
            rejection_counts: dict[str, int] = {}

            # Classify BTC regime once per cycle — gates any regime-scoped statistical setups.
            current_regime: Optional[str] = None
            try:
                btc_candles = fetch_klines_cached("BTCUSDT", "1h", cfg["window_size"], use_cache=False, drop_forming=True)
                if btc_candles and len(btc_candles) >= 220:
                    btc_candle_dicts = [{"close": float(c[3])} for c in btc_candles]
                    current_regime = classify_current_regime(btc_candle_dicts)
                    log.info(f"BTC regime: {current_regime}")
                    # Publish BTC 4-candle pct change so setups using btc_pump_4h / btc_dump_4h can fire.
                    if len(btc_candles) >= 5:
                        from .signals import snapshot as _snap
                        close_now = float(btc_candles[-1][3]); close_4 = float(btc_candles[-5][3])
                        pct_4h = (close_now - close_4) / close_4 * 100.0 if close_4 > 0 else 0.0
                        _snap.set_btc_pct_4h_context({0: pct_4h})
                        log.info(f"BTC 4-candle pct change: {pct_4h:+.2f}%")
            except Exception as exc:
                log.warning(f"Regime classification failed: {exc} — regime-scoped setups disabled this cycle (fail closed)")

            for symbol in SYMBOLS:
                if len(state["positions"]) >= cfg["max_positions"]:
                    break

                # Skip if already have a position on this symbol
                if symbol in state["positions"]:
                    log.info(f"  {symbol}: already in position, skip")
                    continue

                # Cooldown check
                if _in_cooldown(state, symbol, cfg["cooldown_hours"]):
                    log.info(f"  {symbol}: in cooldown, skip")
                    continue

                # Fetch 1000-candle window — CLOSED candles only (drop_forming).
                # Signals must be computed on the same data the analyzer mined
                # and the backtester replays; the forming candle creates
                # intra-hour phantom signals that were never validated.
                candles = fetch_klines_cached(symbol, "1h", cfg["window_size"], use_cache=False, drop_forming=True)
                if not candles or len(candles) < 200:
                    log.warning(f"  {symbol}: insufficient candle data ({len(candles) if candles else 0}), skip")
                    time.sleep(1)
                    continue

                # Generate signal (same function as backtester)
                try:
                    trade_signal = generate_signal(
                        symbol,
                        candles,
                        current_time=now,
                        signal_engine=signal_engine,
                        rulebook_path=validated_path,
                        current_regime=current_regime,
                    )
                except Exception as e:
                    log.error(f"  {symbol}: generate_signal error: {e}", exc_info=True)
                    time.sleep(1)
                    continue

                time.sleep(1.5)  # Rate limit between symbols

                if trade_signal is None or trade_signal.direction == "NEUTRAL":
                    # Get rejection reason from scanner
                    rejection_reason = _last_rejection_reason.get(symbol, "Unknown")
                    log.info(f"  {symbol}: ✗ {rejection_reason}")
                    _bump_rejection_count(rejection_counts, rejection_reason)
                    continue

                # Stash trader-side context on the (frozen) Signal.
                trade_signal = dataclasses.replace(
                    trade_signal,
                    market_regime=current_regime or trade_signal.market_regime,
                )
                audit = _signal_audit_context(trade_signal, signal_engine)
                trade_signal = dataclasses.replace(
                    trade_signal,
                    selected_source=trade_signal.selected_source or audit["source"],
                    setup_name=trade_signal.setup_name or audit["setup_name"],
                )

                score    = trade_signal.score
                strategy = trade_signal.strategy
                threshold = _required_threshold(signal_engine, strategy)

                # ── Log signal details ──
                d = trade_signal.details or {}
                entry_p   = trade_signal.entry_price
                l_score   = trade_signal.long_score
                s_score   = trade_signal.short_score
                regime    = trade_signal.regime or d.get("regime", "?")
                sig_engine = trade_signal.signal_engine or signal_engine
                hybrid_d  = trade_signal.hybrid_details

                log.info(
                    f"  {symbol}: ${entry_p:,.2f} | {trade_signal.direction} "
                    f"score={score:.2f} | engine={sig_engine} strategy={strategy}"
                )

                # ── Technical side ──
                if hybrid_d and hybrid_d.get("technical"):
                    tech_h = hybrid_d["technical"]
                    rsi_val  = d.get("rsi", {}).get("1h", d.get("rsi", "?")) if isinstance(d.get("rsi"), dict) else d.get("rsi", "?")
                    adx_val  = d.get("adx", "?")
                    vol_r    = d.get("vol_ratio", "?")
                    above200 = d.get("above_e200")
                    ema_bull = d.get("ema_bull", False)
                    ema_bear = d.get("ema_bear", False)
                    bb_sq    = d.get("bb_squeeze", False)
                    log.info(
                        f"    [TECHNICAL] {tech_h['direction']} score={tech_h['score']:.2f} "
                        f"L={tech_h['long_score']:.2f} S={tech_h['short_score']:.2f} | "
                        f"RSI={rsi_val} ADX={adx_val} vol={vol_r} "
                        f"EMA200={'above' if above200 else 'below'} "
                        f"bull={ema_bull} bear={ema_bear} squeeze={bb_sq} regime={regime}"
                    )
                elif hybrid_d and hybrid_d.get("technical_reject_reason"):
                    log.info(f"    [TECHNICAL] no signal — {hybrid_d['technical_reject_reason']}")
                elif not hybrid_d and (d.get("rsi") is not None or d.get("adx") is not None):
                    # Pure technical model (not hybrid)
                    rsi_val  = d.get("rsi", {}).get("1h", d.get("rsi", "?")) if isinstance(d.get("rsi"), dict) else d.get("rsi", "?")
                    adx_val  = d.get("adx", "?")
                    vol_r    = d.get("vol_ratio", "?")
                    above200 = d.get("above_e200")
                    ema_bull = d.get("ema_bull", False)
                    ema_bear = d.get("ema_bear", False)
                    bb_sq    = d.get("bb_squeeze", False)
                    log.info(
                        f"    [TECHNICAL] RSI={rsi_val} ADX={adx_val} vol={vol_r} "
                        f"EMA200={'above' if above200 else 'below'} "
                        f"bull={ema_bull} bear={ema_bear} squeeze={bb_sq} regime={regime} | "
                        f"L={l_score:.2f} S={s_score:.2f}"
                    )

                # ── Statistical side ──
                if hybrid_d and hybrid_d.get("statistical"):
                    stat_h = hybrid_d["statistical"]
                    log.info(
                        f"    [STATISTICAL] MATCH {stat_h['direction']} "
                        f"setup={stat_h.get('setup', '?')} template={stat_h.get('template', '?')} "
                        f"conditions={stat_h.get('conditions', [])}"
                    )
                elif hybrid_d and hybrid_d.get("statistical_reject_reason"):
                    log.info(f"    [STATISTICAL] no match — {hybrid_d['statistical_reject_reason']}")
                elif hybrid_d:
                    log.info(f"    [STATISTICAL] no match")

                stat_d = trade_signal.statistical_details
                if stat_d:
                    test_stats = stat_d.get("test_stats", {})
                    train_stats = stat_d.get("train_stats", {})
                    log.info(
                        f"    [STATS] train(wr={train_stats.get('win_rate', '-')}% "
                        f"pf={train_stats.get('profit_factor', '-')} n={train_stats.get('count', '-')}) "
                        f"test(wr={test_stats.get('win_rate', '-')}% "
                        f"pf={test_stats.get('profit_factor', '-')} n={test_stats.get('count', '-')})"
                    )

                # ── Hybrid decision ──
                if hybrid_d:
                    sel = hybrid_d.get("selected", {})
                    log.info(f"    [HYBRID] => {sel.get('source', '?')} ({sel.get('reason', '?')})")

                if threshold is not None and score < threshold:
                    d = trade_signal.details or {}
                    rsi = d.get("rsi", "?")
                    adx = d.get("adx", "?")
                    regime = trade_signal.regime or "?"
                    log.info(
                        f"  {symbol}: ✗ score {score:.2f} < {threshold:.0f} "
                        f"| {regime} | RSI={rsi} ADX={adx}"
                    )
                    _bump_rejection_count(rejection_counts, "Score below threshold")
                    continue

                log.info(
                    f"  {symbol}: >>> ENTRY {trade_signal.direction} "
                    f"TP={trade_signal.tp_pct:.2f}% SL={trade_signal.sl_pct:.2f}% "
                    f"R:R={trade_signal.rr_ratio} ATR={trade_signal.atr}"
                )

                # Execute
                opened = execute_entry(trade_signal, state, client, cfg, dry_run, equity)
                if opened:
                    log.info(f"  {symbol}: position opened")
                else:
                    log.warning(f"  {symbol}: entry failed")

            if rejection_counts:
                log.info(f"Rejection summary: {_format_rejection_summary(rejection_counts)}")

        # ── Summary ────────────────────────────────────────────────
        elapsed = time.time() - cycle_start
        log.info(f"Cycle done in {elapsed:.0f}s | open={len(state['positions'])} | balance=${balance:.2f}")
        time.sleep(max(0, cfg["scan_interval"] - elapsed))


if __name__ == "__main__":
    main()
