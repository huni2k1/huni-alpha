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
  SIGNAL_MODEL          technical | statistical | statistical_curated
  RISK_PER_TRADE_PCT    default 1.5
  MAX_POSITIONS         default 3
  SCAN_INTERVAL_SEC     default 300 (5 minutes)
"""

import os
import sys
import json
import time
import atexit
import logging
import requests
import signal
from datetime import datetime, timedelta, timezone
from typing import Optional

# ── Import scanner internals ─────────────────────────────────────
try:
    from . import scanner
    from .scanner import (
        generate_signal,
        fetch_klines_cached,
        send_telegram,
        SYMBOLS,
        SIGNAL_THRESHOLD_TREND,
        SIGNAL_THRESHOLD_BREAKOUT,
        VALID_SIGNAL_MODELS,
        VALIDATED_SETUPS_PATH,
        CURATED_VALIDATED_SETUPS_PATH,
    )
    from .binance_client import BinanceClient
except ImportError:
    import scanner
    from scanner import (
        generate_signal,
        fetch_klines_cached,
        send_telegram,
        SYMBOLS,
        SIGNAL_THRESHOLD_TREND,
        SIGNAL_THRESHOLD_BREAKOUT,
        VALID_SIGNAL_MODELS,
        VALIDATED_SETUPS_PATH,
        CURATED_VALIDATED_SETUPS_PATH,
    )
    from binance_client import BinanceClient

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────
_TRADER_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "trader.json"
)


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
        "signal_model":      _get("SIGNAL_MODEL", "technical").strip().lower(),
        "risk_pct":          float(_get("RISK_PER_TRADE_PCT", 1.5)),
        "max_positions":     int(_get("MAX_POSITIONS", 3)),
        "scan_interval":     int(_get("SCAN_INTERVAL_SEC", 300)),
        "cooldown_hours":    int(_get("COOLDOWN_HOURS", 24)),
        "timeout_hours":     int(_get("TIMEOUT_HOURS", 180)),
        "max_drawdown_pct":  float(_get("MAX_DRAWDOWN_PCT", 25.0)),
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

_EMPTY_STATE = {
    "positions": {},        # symbol → position dict
    "last_signal": {},      # symbol → ISO timestamp of last signal
    "peak_equity": None,    # float — for circuit breaker
    "circuit_breaker_until": None,  # ISO timestamp or None
    "trade_log": [],        # closed trade records
}


def load_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            s = json.load(f)
            # Ensure all keys present (forward-compat with older state files)
            for k, v in _EMPTY_STATE.items():
                s.setdefault(k, v)
            return s
    except Exception:
        return dict(_EMPTY_STATE)


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
    signal_model: str,
    price_precision: int,
    tp_price: Optional[float] = None,
    sl_price: Optional[float] = None,
    tp_order_id: Optional[str] = None,
    sl_order_id: Optional[str] = None,
    protection_status: str = "pending",
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
        "signal_model": signal_model,
        "protection_status": protection_status,
    }
    save_state(state)


# ─────────────────────────────────────────────────────────────────
# POSITION SIZING  (mirrors backtester logic)
# ─────────────────────────────────────────────────────────────────
def size_position(
    balance: float,
    risk_pct: float,
    sl_pct: float,
    entry_price: float,
    qty_precision: int,
    min_qty: float,
    min_notional: float,
    max_positions: int = 3,
) -> Optional[float]:
    """
    Risk-based position sizing — same formula as backtester.

    risk_amount    = balance * risk_pct%
    position_usdt  = risk_amount / sl_pct_decimal
    quantity       = position_usdt / entry_price, capped at balance/max_positions

    Example with $100 balance, max_positions=3:
      - Trade 1: max $33.33 per position
      - Trade 2: max $33.33 per position
      - Trade 3: max $33.33 per position
      - Total exposure: ~$100

    Returns quantity in base asset, or None if below minimum.
    """
    if sl_pct <= 0 or entry_price <= 0:
        return None

    risk_amount = balance * (risk_pct / 100)
    position_usdt = risk_amount / (sl_pct / 100)
    position_usdt = min(position_usdt, balance / max_positions)   # balance divided by max positions

    quantity = position_usdt / entry_price
    quantity = round(quantity, qty_precision)

    # Check minimums
    if quantity < min_qty:
        log.warning(f"Quantity {quantity} below min_qty {min_qty}")
        return None
    if quantity * entry_price < min_notional:
        log.warning(f"Notional {quantity * entry_price:.2f} below min_notional {min_notional}")
        return None

    return quantity


# ─────────────────────────────────────────────────────────────────
# THRESHOLD  (mirrors backtester logic)
# ─────────────────────────────────────────────────────────────────
_STATISTICAL_MODELS = {"statistical", "statistical_curated", "statistical_wide_short_rsi28"}


def _required_threshold(signal_model: str, strategy: str) -> Optional[float]:
    """Return minimum score threshold, or None to bypass (setup membership is the gate)."""
    if signal_model in _STATISTICAL_MODELS:
        return None
    # Hybrid model: statistical signals bypass threshold, technical signals need score gate
    if signal_model == "hybrid_technical_wide_short_rsi28":
        if "statistical" in strategy or "rsi28" in strategy:
            return None  # Statistical side: setup membership is the gate
        if "breakout" in strategy:
            return SIGNAL_THRESHOLD_BREAKOUT
        return SIGNAL_THRESHOLD_TREND
    if "breakout" in strategy:
        return SIGNAL_THRESHOLD_BREAKOUT
    return SIGNAL_THRESHOLD_TREND


# ─────────────────────────────────────────────────────────────────
# COOLDOWN HELPERS
# ─────────────────────────────────────────────────────────────────
def _in_cooldown(state: dict, symbol: str, cooldown_hours: int) -> bool:
    last_str = state["last_signal"].get(symbol)
    if not last_str:
        return False
    last_dt = datetime.fromisoformat(last_str)
    return (datetime.now(timezone.utc) - last_dt).total_seconds() < cooldown_hours * 3600


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
                }
                state["trade_log"].append(trade_record)

                emoji = "✅" if pnl_pct > 0 else "❌"
                dir_emoji = "🟢" if direction == "LONG" else "🔴"
                log.info(f"  {emoji} CLOSED {symbol} {exit_reason}: {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
                send_telegram(
                    f"{emoji} <b>{exit_reason}</b> {dir_emoji} {symbol.replace('USDT','')} "
                    f"{'LONG' if direction == 'LONG' else 'SHORT'}\n"
                    f"Entry: <code>${entry:,.4f}</code> → Exit: <code>${exit_price:,.4f}</code>\n"
                    f"PnL: <b>{pnl_pct:+.2f}%</b> (${pnl_usd:+.2f})\n"
                    f"Age: {age_hours:.1f}h"
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
    balance: float,
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
    symbol    = signal["symbol"]
    direction = signal["direction"]
    entry_p   = signal["entry_price"]
    tp_p      = signal["tp"]
    sl_p      = signal["sl"]
    sl_pct    = abs(signal["sl_pct"])
    tp_pct    = abs(signal["tp_pct"])
    strategy  = signal.get("strategy", "trend_pullback")
    score     = signal.get("score", 0)

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
        balance, cfg["risk_pct"], sl_pct, entry_p, qty_prec, min_qty, min_notional,
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
            signal_model=cfg["signal_model"],
            price_precision=price_prec,
            protection_status="pending",
        )

        # 3. Re-anchor TP/SL to actual fill price (same ATR distance)
        if direction == "LONG":
            tp_distance = tp_p - entry_p
            sl_distance = entry_p - sl_p
        else:
            tp_distance = entry_p - tp_p
            sl_distance = sl_p - entry_p

        if direction == "LONG":
            tp_p = fill_price + tp_distance
            sl_p = fill_price - sl_distance
        else:
            tp_p = fill_price - tp_distance
            sl_p = fill_price + sl_distance

        # 4. Place TP order
        tp_order_id = client.place_tp_order(symbol, close_side, tp_p, price_prec)
        if not tp_order_id:
            log.error(f"  {symbol}: TP order failed — emergency closing position")
            result = client.place_market_close(symbol, close_side, quantity)
            if not result:
                log.error(f"  {symbol}: EMERGENCY CLOSE ALSO FAILED — position open without TP/SL!")
                send_telegram(f"🚨 {symbol} EMERGENCY CLOSE FAILED — manual intervention needed!")
            state["positions"].pop(symbol, None)
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
        signal_model=cfg["signal_model"],
        price_precision=price_prec if not dry_run else 4,
        tp_price=tp_p,
        sl_price=sl_p,
        tp_order_id=tp_order_id,
        sl_order_id=sl_order_id,
        protection_status="armed",
    )
    _set_cooldown(state, symbol)
    save_state(state)

    dir_emoji = "🟢" if direction == "LONG" else "🔴"
    dry_tag = " [DRY RUN]" if dry_run else ""
    send_telegram(
        f"{dir_emoji} <b>NEW TRADE{dry_tag}</b> {symbol.replace('USDT','')} "
        f"{'LONG' if direction == 'LONG' else 'SHORT'}\n"
        f"Entry: <code>${fill_price:,.4f}</code>\n"
        f"TP: <code>${tp_p:,.4f}</code> (+{tp_pct:.2f}%)\n"
        f"SL: <code>${sl_p:,.4f}</code> (-{sl_pct:.2f}%)\n"
        f"Size: ${position_usdt:.0f} ({quantity} {symbol.replace('USDT','')})\n"
        f"Score: {score:.2f} | Strategy: {strategy}"
    )

    return True


# ─────────────────────────────────────────────────────────────────
# STARTUP RECONCILIATION
# ─────────────────────────────────────────────────────────────────
def reconcile_with_binance(state: dict, client: BinanceClient):
    """
    On startup: compare state positions vs Binance actual positions.
    Warns on mismatch — does not auto-close anything.
    """
    if not state["positions"]:
        return

    live_positions = {p["symbol"] for p in client.get_open_positions()}
    state_symbols = set(state["positions"].keys())

    stale = state_symbols - live_positions
    unknown = live_positions - state_symbols

    if stale:
        log.warning(
            f"State has positions Binance doesn't: {stale}. "
            f"These may have been closed externally. Removing from state."
        )
        for sym in stale:
            del state["positions"][sym]
        save_state(state)

    if unknown:
        log.warning(
            f"Binance has positions not in state: {unknown}. "
            f"Manual intervention may be needed."
        )
        send_telegram(
            f"⚠️ <b>Position mismatch on startup</b>\n"
            f"Binance has open positions not tracked by bot: {', '.join(unknown)}\n"
            f"Please review manually."
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

    cfg = _load_config()
    dry_run = cfg["dry_run"]

    signal_model = cfg["signal_model"]
    if signal_model not in VALID_SIGNAL_MODELS:
        signal_model = "technical"

    validated_path = (
        CURATED_VALIDATED_SETUPS_PATH
        if signal_model == "statistical_curated"
        else VALIDATED_SETUPS_PATH
    )

    mode_tag = "[DRY RUN] " if dry_run else "[LIVE] "
    log.info("=" * 60)
    log.info(f"Trader {mode_tag}starting (PID: {os.getpid()})")
    log.info(f"Signal model:   {signal_model}")
    log.info(f"Risk per trade: {cfg['risk_pct']}%")
    log.info(f"Max positions:  {cfg['max_positions']}")
    log.info(f"Cooldown:       {cfg['cooldown_hours']}h")
    log.info(f"Symbols:        {', '.join(SYMBOLS)}")
    log.info("=" * 60)

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

    send_telegram(
        f"🤖 <b>Trader Online {mode_tag}</b>\n"
        f"Model: {signal_model} | Risk: {cfg['risk_pct']}% | Max pos: {cfg['max_positions']}\n"
        f"Symbols: {', '.join(s.replace('USDT','') for s in SYMBOLS)}\n"
        f"Testnet: {client.testnet}"
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

                # Fetch 1000-candle window
                candles = fetch_klines_cached(symbol, "1h", cfg["window_size"], use_cache=False)
                if not candles or len(candles) < 200:
                    log.warning(f"  {symbol}: insufficient candle data ({len(candles) if candles else 0}), skip")
                    time.sleep(1)
                    continue

                # Generate signal (same function as backtester)
                try:
                    trade_signal = generate_signal(
                        symbol,
                        candles,
                        include_fundamentals=False,
                        include_news=False,
                        current_time=now,
                        signal_model=signal_model,
                        validated_setups_path=validated_path,
                    )
                except Exception as e:
                    log.error(f"  {symbol}: generate_signal error: {e}", exc_info=True)
                    time.sleep(1)
                    continue

                time.sleep(1.5)  # Rate limit between symbols

                if trade_signal is None or trade_signal["direction"] == "NEUTRAL":
                    # Get rejection reason from scanner
                    rejection_reason = scanner._last_rejection_reason.get(symbol, "Unknown")
                    log.info(f"  {symbol}: ✗ {rejection_reason}")
                    continue

                score    = trade_signal["score"]
                strategy = trade_signal.get("strategy", "trend_pullback")
                threshold = _required_threshold(signal_model, strategy)

                # ── Log signal details ──
                d = trade_signal.get("details", {})
                entry_p   = trade_signal.get("entry_price", 0)
                l_score   = trade_signal.get("long_score", 0)
                s_score   = trade_signal.get("short_score", 0)
                regime    = trade_signal.get("regime") or d.get("regime", "?")
                sig_model = trade_signal.get("signal_model", signal_model)
                hybrid_d  = trade_signal.get("hybrid_details")

                log.info(
                    f"  {symbol}: ${entry_p:,.2f} | {trade_signal['direction']} "
                    f"score={score:.2f} | model={sig_model} strategy={strategy}"
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

                stat_d = trade_signal.get("statistical_details")
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
                    d = trade_signal.get("details", {})
                    rsi = d.get("rsi", "?")
                    adx = d.get("adx", "?")
                    regime = trade_signal.get("regime", "?")
                    log.info(
                        f"  {symbol}: ✗ score {score:.2f} < {threshold:.0f} "
                        f"| {regime} | RSI={rsi} ADX={adx}"
                    )
                    continue

                log.info(
                    f"  {symbol}: >>> ENTRY {trade_signal['direction']} "
                    f"TP={trade_signal['tp_pct']:.2f}% SL={trade_signal['sl_pct']:.2f}% "
                    f"R:R={trade_signal.get('rr_ratio', '?')} ATR={trade_signal.get('atr', '?')}"
                )

                # Execute
                opened = execute_entry(trade_signal, state, client, cfg, dry_run, balance)
                if opened:
                    log.info(f"  {symbol}: position opened")
                else:
                    log.warning(f"  {symbol}: entry failed")

        # ── Summary ────────────────────────────────────────────────
        elapsed = time.time() - cycle_start
        log.info(f"Cycle done in {elapsed:.0f}s | open={len(state['positions'])} | balance=${balance:.2f}")
        time.sleep(max(0, cfg["scan_interval"] - elapsed))


if __name__ == "__main__":
    main()
