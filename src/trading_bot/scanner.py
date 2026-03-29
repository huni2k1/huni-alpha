#!/usr/bin/env python3
"""
Crypto Market Scanner — Multi-Regime Adaptive Scoring

Architecture: generate_signal() is the SINGLE SOURCE OF TRUTH.
Both live scanner and backtester consume the same signal object.
Signal includes: direction, score, entry, TP (ATR-based), SL (ATR-based), regime, strategy.

Regimes:
  - Trending (ADX>25): Trend pullback strategy — buy dips in a strong trend
  - Ranging (ADX<20):  [disabled — use trend pullback instead]
  - Breakout:          Squeeze release strategy — catch compression breakouts
  - Weak trend (20-25): Trend pullback (safe mode)

TP/SL per strategy:
  - Trend pullback: 1.5x ATR SL, 2:1 R:R
  - Mean reversion: 1.0x ATR SL, 1.5:1 R:R
  - Breakout:       2.0x ATR SL, 2.5:1 R:R

Thresholds: 5+ Watch | 6+ Entry | 8+ High Confidence
"""

import os
import sys
import json
import time
import math
import logging
import requests
import numpy as np
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
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
# CONFIG — Load sensitive data from environment or config file
# ─────────────────────────────────────────────────────────────────
_tg_token = os.environ.get("TELEGRAM_TOKEN", "")
_tg_chat = os.environ.get("TELEGRAM_CHAT", "")

# Fall back to config/telegram.json if env vars not set
if not _tg_token or not _tg_chat:
    try:
        _config_path = os.path.join(os.path.dirname(__file__), "..", "config", "telegram.json")
        with open(_config_path) as f:
            _tg_config = json.load(f)
            _tg_token = _tg_token or _tg_config.get("token", "")
            _tg_chat = _tg_chat or _tg_config.get("chat_id", "")
    except Exception:
        pass

TELEGRAM_TOKEN  = _tg_token
TELEGRAM_CHAT   = _tg_chat
SCAN_INTERVAL   = 300          # 5 minutes
ALERT_COOLDOWN  = 7200         # 2 hours (per symbol per direction)

ALERT_THRESHOLD_SOFT = 5.5     # Watch list (Telegram only, no position)
ALERT_THRESHOLD_OPTB = 6.0     # Entry (take position) — optimized for 57 trades/month (backtest validated)
ALERT_THRESHOLD_HARD = 7.5     # High Confidence (full position)

# ─────────────────────────────────────────────────────────────────
# STRATEGY PARAMETERS (shared with backtester)
# ─────────────────────────────────────────────────────────────────
SIGNAL_THRESHOLD_TREND = 7.0        # Min score for trend_pullback signals
SIGNAL_THRESHOLD_BREAKOUT = 6.0     # Min score for breakout signals
MAX_OPEN_POSITIONS = 3              # Maximum concurrent trades
SIGNAL_COOLDOWN_CANDLES = 48        # Min 1h-candles between signals per symbol (48h = 2 days, prevents re-entry after SL)
RISK_PER_TRADE_PCT = 1.5            # Risk percentage of account per trade (1.5% historically optimal)

LOG_FILE        = os.environ.get("SCANNER_LOG", "/tmp/scanner.log")
DEBUG_LOG_FILE  = os.environ.get("SCANNER_DEBUG_LOG",
    LOG_FILE.replace(".log", "-debug.log"))
PID_FILE        = "/tmp/scanner.pid"
STATE_FILE      = "/tmp/scanner-state.json"

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
]

# ─────────────────────────────────────────────────────────────────
# LOGGING — with rotation to keep last 10 scan cycles
# ─────────────────────────────────────────────────────────────────
from logging.handlers import RotatingFileHandler

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
log = logging.getLogger("scanner")
log.setLevel(logging.INFO)
log.propagate = False
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
# RotatingFileHandler: keep last ~50MB (typical 50-100 scan cycles)
_fh = RotatingFileHandler(LOG_FILE, maxBytes=50*1024*1024, backupCount=10)
_fh.setFormatter(_fmt)
log.addHandler(_fh)
if sys.stdout.isatty():
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    log.addHandler(_sh)

# Debug logger — detailed reasoning and scores (with rotation)
dbg = logging.getLogger("scanner.debug")
dbg.setLevel(logging.DEBUG)
dbg.propagate = False
_dfh = RotatingFileHandler(DEBUG_LOG_FILE, maxBytes=50*1024*1024, backupCount=10)
_dfh.setFormatter(_fmt)
dbg.addHandler(_dfh)


# ─────────────────────────────────────────────────────────────────
# STATE  (cooldown tracking)
# ─────────────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"alerts": {}}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def can_alert(state: dict, symbol: str, direction: str, tier: str = "ENTRY") -> bool:
    """Check if alert can fire (separate cooldown for WATCH vs ENTRY)."""
    key = f"{symbol}_{direction}_{tier}"
    last = state["alerts"].get(key, 0)
    return (time.time() - last) > ALERT_COOLDOWN

def mark_alert(state: dict, symbol: str, direction: str, tier: str = "ENTRY"):
    """Mark alert as fired (separate tracking for WATCH vs ENTRY)."""
    state["alerts"][f"{symbol}_{direction}_{tier}"] = time.time()

def is_whipsaw(state: dict, symbol: str, new_direction: str) -> bool:
    """Check if signal direction changed in last 5 minutes (prevent false reversals)."""
    if "signal_history" not in state:
        state["signal_history"] = {}

    key = symbol
    last_signal = state["signal_history"].get(key)
    now = time.time()

    if last_signal and now - last_signal["ts"] < 300:  # Within 5 minutes
        if last_signal["direction"] != new_direction and new_direction != "NEUTRAL":
            dbg.debug(f"[{symbol}] WHIPSAW DETECTED: was {last_signal['direction']}, now {new_direction} (in {now - last_signal['ts']:.0f}s)")
            return True

    # Record this signal
    state["signal_history"][key] = {"direction": new_direction, "ts": now}
    return False

def mark_signal(state: dict, symbol: str, direction: str):
    """Record signal for whipsaw detection."""
    if "signal_history" not in state:
        state["signal_history"] = {}
    state["signal_history"][symbol] = {"direction": direction, "ts": time.time()}


# ─────────────────────────────────────────────────────────────────
# HTTP HELPERS
# ─────────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CryptoScanner/2.0"})

def get(url: str, params: dict = None, timeout: int = 10) -> Optional[dict]:
    """Fetch with retry logic for transient failures."""
    max_retries = 2
    for attempt in range(max_retries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < max_retries - 1:
                log.debug(f"GET {url} attempt {attempt+1} failed ({e}), retrying...")
                time.sleep(0.5)  # Brief delay before retry
            else:
                log.debug(f"GET {url} failed after {max_retries} attempts: {e}")
    return None


# ─────────────────────────────────────────────────────────────────
# BINANCE DATA
# ─────────────────────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str, limit: int) -> Optional[list]:
    """Fetch klines with pagination support (Binance API limit is 1000)."""
    # For 1000 or fewer candles, fetch in one call (no pagination needed)
    if limit <= 1000:
        data = get("https://api.binance.com/api/v3/klines",
                   {"symbol": symbol, "interval": interval, "limit": limit})
        if not data:
            return None
        if len(data) < int(limit * 0.95):
            log.warning(f"  {symbol}: Incomplete data — requested {limit} candles, got {len(data)}")
            return None
        # Convert to [open, high, low, close, volume] format
        return [[float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
                for c in data]

    # For > 1000 candles, paginate by repeatedly fetching latest 1000
    # and using endTime to move backward in time
    all_candles = []
    all_raw = []  # Keep raw data for gap detection
    remaining = limit
    end_time = None

    while remaining > 0:
        batch_size = min(1000, remaining)
        params = {"symbol": symbol, "interval": interval, "limit": batch_size}

        # Set endTime to get older candles (if not first iteration)
        if end_time is not None:
            params["endTime"] = end_time

        data = get("https://api.binance.com/api/v3/klines", params)
        if not data or len(data) == 0:
            break  # Stop if API fails or no more data

        # Binance returns [openTime, open, high, low, close, volume, ...]
        # Convert to [open, high, low, close, volume] format
        batch = [[float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
                 for c in data]
        batch_raw = data

        # Prepend batch (oldest first)
        all_candles = batch + all_candles
        all_raw = batch_raw + all_raw
        remaining -= len(batch)

        # Set endTime for next iteration (milliseconds before oldest candle's openTime)
        if data:
            oldest_open_time_ms = int(data[0][0])
            end_time = oldest_open_time_ms - 1

        # Stop if we got fewer than requested (at history limit)
        if len(batch) < batch_size:
            break

    if not all_candles:
        return None

    # Completeness check: did we get close to what was requested?
    min_expected = int(limit * 0.95)
    if len(all_candles) < min_expected:
        log.warning(f"  {symbol}: Incomplete data — requested {limit} candles, got {len(all_candles)} (min {min_expected})")
        return None

    # Gap detection: check for missing candles (using openTime from raw data)
    gap_count = 0
    interval_ms = 3_600_000  # 1h default
    if interval == "4h":
        interval_ms = 4 * 3_600_000
    for i in range(1, len(all_raw)):
        diff_ms = int(all_raw[i][0]) - int(all_raw[i-1][0])
        if diff_ms > interval_ms * 1.5:
            gap_hours = diff_ms / 3_600_000
            gap_count += 1
            if gap_hours > 24:
                log.warning(f"  {symbol}: Large gap detected ({gap_hours:.1f}h) — data may be unreliable")

    if gap_count > 0:
        log.warning(f"  {symbol}: {gap_count} gap(s) in candle data")

    return all_candles


def fetch_klines_cached(symbol: str, interval: str, limit: int,
                       use_cache: bool = True) -> Optional[list]:
    """
    Fetch klines with local disk caching for speed.

    For backtesting: caches entire date ranges to avoid repeated API calls.
    For live scanning: uses cache if available, falls back to API.

    Args:
        symbol: Trading pair (e.g., "BTCUSDT")
        interval: Candle interval ("1h", "4h")
        limit: Number of candles to fetch
        use_cache: Enable caching (default: True)

    Returns:
        List of candles [[open, high, low, close, volume], ...] or None
    """
    if not use_cache:
        return fetch_klines(symbol, interval, limit)

    # Calculate date range from limit
    now = datetime.now(timezone.utc)
    candles_per_day = 24 if interval == "1h" else 6  # approximate
    days_needed = (limit + candles_per_day - 1) // candles_per_day
    start_date = (now - timedelta(days=days_needed)).date()
    end_date = now.date()

    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    # Try to load from cache
    cached_candles = candle_cache.load_from_cache(symbol, interval, start_str, end_str)
    if cached_candles is not None:
        log.debug(f"  {symbol}: Loaded {len(cached_candles)} candles from cache")
        return cached_candles[-limit:] if len(cached_candles) > limit else cached_candles

    # Not in cache — fetch from API
    log.debug(f"  {symbol}: Cache miss, fetching from Binance API...")
    candles = fetch_klines(symbol, interval, limit)

    if candles is not None:
        # Save to cache for future use
        candle_cache.save_to_cache(symbol, interval, start_str, end_str, candles)
        log.debug(f"  {symbol}: Cached {len(candles)} candles for {start_str}–{end_str}")

    return candles


# ─────────────────────────────────────────────────────────────────
# TECHNICAL INDICATORS
# ─────────────────────────────────────────────────────────────────
def rsi(closes: list, period: int = 14) -> float:
    """RSI with Wilder smoothing (standard RSI matching TradingView/Binance)."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))

    avg_g = np.mean(gains[:period])
    avg_l = np.mean(losses[:period])

    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period

    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def ema(values: list, period: int) -> list:
    """
    EMA with standard SMA seed (matching TradingView behavior).
    Returns fewer values than input (shortened by seed_period-1 elements).
    Callers should use the last value: ema_val = ema(closes, 200)[-1]
    """
    if not values:
        return []
    result = []
    k = 2 / (period + 1)

    seed_period = min(period, len(values))
    seed_value = np.mean(values[:seed_period])

    for i, v in enumerate(values):
        if i < seed_period:
            if i == seed_period - 1:
                result.append(seed_value)
        else:
            result.append(v * k + result[-1] * (1 - k))

    return result


def macd(closes: list, fast=12, slow=26, signal=9):
    """MACD with proper EMA alignment (fast & slow aligned from the end)."""
    if len(closes) < slow + signal:
        return 0, 0, 0
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)

    # FIX: Align EMAs from the end — fast EMA has more values than slow.
    # Without alignment, zip pairs mismatched time positions.
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [ema_fast[offset + i] - ema_slow[i] for i in range(len(ema_slow))]

    sig_line = ema(macd_line, signal)

    # Align MACD line with signal line the same way
    offset2 = len(macd_line) - len(sig_line)
    hist = [macd_line[offset2 + i] - sig_line[i] for i in range(len(sig_line))]

    return macd_line[-1], sig_line[-1], hist[-1]


def bollinger(closes: list, period=20, std_mult=2):
    if len(closes) < period:
        return closes[-1], closes[-1] * 1.02, closes[-1] * 0.98
    window = closes[-period:]
    mid = np.mean(window)
    std = np.std(window)
    return mid, mid + std_mult * std, mid - std_mult * std


def ema_alignment(closes: list):
    """Returns (ema9, ema21, ema50, bullish_aligned, bearish_aligned)"""
    if len(closes) < 50:
        return None, None, None, False, False
    e9  = ema(closes, 9)[-1]
    e21 = ema(closes, 21)[-1]
    e50 = ema(closes, 50)[-1]
    bull = e9 > e21 > e50
    bear = e9 < e21 < e50
    return e9, e21, e50, bull, bear


def volume_ratio(volumes: list, period=20) -> float:
    if len(volumes) < period + 1:
        return 1.0
    avg = np.mean(volumes[-period - 1:-1])
    return volumes[-1] / avg if avg > 0 else 1.0


def adx(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """Average Directional Index — measures trend strength (0-100).
    >25 = trending market, <20 = choppy/sideways."""
    if len(highs) < period * 2:
        return 0.0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(highs)):
        h_diff = highs[i] - highs[i - 1]
        l_diff = lows[i - 1] - lows[i]
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)

    def wilder(vals, p):
        result = [sum(vals[:p])]
        for v in vals[p:]:
            result.append(result[-1] - result[-1] / p + v)
        return result

    atr_s   = wilder(trs,       period)
    pdm_s   = wilder(plus_dm,   period)
    mdm_s   = wilder(minus_dm,  period)
    dx_vals = []
    for a, p, m in zip(atr_s, pdm_s, mdm_s):
        if a == 0:
            dx_vals.append(0.0)
            continue
        pdi = 100 * p / a
        mdi = 100 * m / a
        dx_vals.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0)

    return float(np.mean(dx_vals[-period:]))


# ─────────────────────────────────────────────────────────────────
# SUPPORT / RESISTANCE  (swing highs/lows on last 10 1h candles)
# ─────────────────────────────────────────────────────────────────
def support_resistance(candles_4h: list, price: float):
    """Returns (near_support, near_resistance, dist_support_pct, dist_resist_pct)"""
    if len(candles_4h) < 10:
        return False, False, 999, 999

    recent = candles_4h[-10:]
    highs = [c[1] for c in recent]
    lows  = [c[2] for c in recent]
    resistance = max(highs)
    support    = min(lows)

    dist_r = (resistance - price) / price * 100
    dist_s = (price - support)    / price * 100

    near_support    = 0 < dist_s < 1.5
    near_resistance = 0 < dist_r < 1.5

    return near_support, near_resistance, dist_s, dist_r


# ─────────────────────────────────────────────────────────────────
# MARKET STRUCTURE  (higher highs / lower lows)
# ─────────────────────────────────────────────────────────────────
def market_structure(candles_4h: list):
    """Returns (higher_highs, lower_lows) bool flags."""
    if len(candles_4h) < 6:
        return False, False
    highs = [c[1] for c in candles_4h[-6:]]
    lows  = [c[2] for c in candles_4h[-6:]]
    hh = all(highs[i] > highs[i-1] for i in range(1, len(highs)))
    ll = all(lows[i]  < lows[i-1]  for i in range(1, len(lows)))
    return hh, ll


# ─────────────────────────────────────────────────────────────────
# REGIME DETECTION — What kind of market are we in?
# ─────────────────────────────────────────────────────────────────
def bollinger_bandwidth(closes: list, period: int = 20, std_mult: float = 2.0) -> tuple:
    """Returns (current_bandwidth_pct, is_squeeze).
    Bandwidth = (upper - lower) / mid * 100.
    Squeeze = bandwidth below 20th percentile of last 100 candles."""
    if len(closes) < max(period, 100):
        return 5.0, False

    bw_history = []
    for i in range(100, 0, -1):
        idx = len(closes) - i
        if idx < period:
            continue
        window = closes[idx - period:idx]
        mid = np.mean(window)
        std = np.std(window)
        if mid > 0:
            bw_history.append((std * std_mult * 2) / mid * 100)

    mid_now, upper_now, lower_now = bollinger(closes, period, std_mult)
    bw_now = (upper_now - lower_now) / mid_now * 100 if mid_now > 0 else 5.0

    is_squeeze = False
    if bw_history:
        threshold = np.percentile(bw_history, 20)
        is_squeeze = bw_now < threshold

    return round(bw_now, 2), is_squeeze


def precompute_indicators_for_all_candles(candles: list) -> dict:
    """
    Pre-compute cheap indicators (RSI, MACD, EMA) for every candle.

    This optimization caches indicator values that are expensive to compute
    repeatedly. Expected 1.5-2x speedup (ADX/Bollinger still computed on-demand).

    Args:
        candles: OHLCV format [[open, high, low, close, volume], ...]

    Returns:
        Dict mapping candle index to pre-computed indicators dict.

    Note:
        ADX and Bollinger Bandwidth are computed on-demand (too expensive to pre-compute).
        Volume ratio is fast to compute, so also done on-demand.
    """
    if len(candles) < 50:
        return {}

    closes = [c[3] for c in candles]
    volumes = [c[4] for c in candles]

    indicators_cache = {}

    # Pre-compute full series for EMA (these are fast and heavily used)
    ema_9_series = ema(closes, 9)
    ema_21_series = ema(closes, 21)
    ema_50_series = ema(closes, 50)
    ema_200_series = ema(closes, min(800, len(closes)))

    # Pre-compute RSI series (Wilder smoothing)
    rsi_series = []
    if len(closes) >= 15:
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i - 1]
            gains.append(max(d, 0))
            losses.append(max(-d, 0))

        avg_g = np.mean(gains[:14])
        avg_l = np.mean(losses[:14])

        # Prepend dummy values for the initial period
        rsi_series = [50.0] * 14

        for i in range(14, len(gains)):
            avg_g = (avg_g * 13 + gains[i]) / 14
            avg_l = (avg_l * 13 + losses[i]) / 14
            if avg_l == 0:
                rsi_series.append(100.0)
            else:
                rs = avg_g / avg_l
                rsi_series.append(100 - (100 / (1 + rs)))

    # Pre-compute MACD series (cheap relative to ADX)
    ema_12 = ema(closes, 12)
    ema_26 = ema(closes, 26)
    offset = len(ema_12) - len(ema_26)
    macd_line_series = [ema_12[offset + i] - ema_26[i] for i in range(len(ema_26))]
    macd_signal_series = ema(macd_line_series, 9)

    # Align histogram
    offset2 = len(macd_line_series) - len(macd_signal_series)
    macd_hist_series = [macd_line_series[offset2 + i] - macd_signal_series[i]
                       for i in range(len(macd_signal_series))]

    # Build cache using pre-computed series (no expensive per-position calculations)
    for t in range(50, len(candles)):
        # Index into pre-computed series
        idx_rsi = min(t, len(rsi_series) - 1)
        rsi_val = rsi_series[idx_rsi] if idx_rsi >= 0 else 50.0

        # MACD: align to current position
        idx_macd = t - (len(closes) - len(macd_line_series))
        if idx_macd >= 0 and idx_macd < len(macd_line_series):
            macd_line_val = macd_line_series[idx_macd]
            idx_signal = idx_macd - (len(macd_line_series) - len(macd_signal_series))
            macd_signal_val = macd_signal_series[idx_signal] if idx_signal >= 0 else 0
            idx_hist = idx_macd - (len(macd_line_series) - len(macd_hist_series))
            macd_hist_val = macd_hist_series[idx_hist] if idx_hist >= 0 else 0
            macd_hist_prev = macd_hist_series[idx_hist - 1] if idx_hist > 0 and idx_hist < len(macd_hist_series) else macd_hist_val
        else:
            macd_line_val = macd_signal_val = macd_hist_val = macd_hist_prev = 0

        # EMA alignment (index into pre-computed series)
        idx_e9 = t - (len(closes) - len(ema_9_series))
        idx_e21 = t - (len(closes) - len(ema_21_series))
        idx_e50 = t - (len(closes) - len(ema_50_series))
        idx_e200 = t - (len(closes) - len(ema_200_series))

        e9 = ema_9_series[idx_e9] if 0 <= idx_e9 < len(ema_9_series) else None
        e21 = ema_21_series[idx_e21] if 0 <= idx_e21 < len(ema_21_series) else None
        e50 = ema_50_series[idx_e50] if 0 <= idx_e50 < len(ema_50_series) else None
        e200 = ema_200_series[idx_e200] if 0 <= idx_e200 < len(ema_200_series) else None

        bull_align = e9 and e21 and e50 and (e9 > e21 > e50)
        bear_align = e9 and e21 and e50 and (e9 < e21 < e50)
        above_e200 = e200 and (closes[t] > e200)
        below_e200 = e200 and (closes[t] < e200)

        # Cache the pre-computed values
        indicators_cache[t] = {
            'rsi': rsi_val,
            'macd_line': macd_line_val,
            'macd_signal': macd_signal_val,
            'macd_hist': macd_hist_val,
            'macd_hist_prev': macd_hist_prev,
            'e9': e9,
            'e21': e21,
            'e50': e50,
            'e200': e200,
            'bull_align': bull_align,
            'bear_align': bear_align,
            'above_e200': above_e200,
            'below_e200': below_e200,
            # Note: ADX, Bollinger, Volume Ratio computed on-demand in score_technical
        }

    return indicators_cache


def detect_regime(adx_val: float, bb_squeeze: bool, vol_r: float,
                  closes: list) -> str:
    """
    Classify current market regime:
    - 'trending':  ADX > 25, clear directional movement
    - 'breakout':  Bollinger squeeze releasing with volume spike
    - 'weak_trend': ADX 20-25, transitional (scored with softened trend logic)

    NOTE: Mean reversion disabled (was causing losses in 2024-2025).
    All ADX < 20 regimes now use weak_trend (trend pullback logic).
    """
    # Breakout: squeeze + volume explosion
    if bb_squeeze and vol_r > 1.5:
        return "breakout"

    # Strong trend
    if adx_val >= 25:
        return "trending"

    # Weak/forming trend (includes former "ranging" — now uses trend pullback)
    if adx_val >= 20:
        return "weak_trend"

    # ADX < 20 (was "ranging"): Now use weak trend instead of mean reversion
    return "weak_trend"


# ─────────────────────────────────────────────────────────────────
# STRATEGY 1: TREND PULLBACK (original V3, softened penalties)
# Best when: ADX > 25, clean EMA alignment
# ─────────────────────────────────────────────────────────────────
def score_trend_pullback(closes: list, volumes: list, rsi_val: float,
                         macd_line: float, signal_line: float,
                         hist_curr: float, hist_prev: float,
                         bull_align: bool, bear_align: bool,
                         adx_val: float = 25, above_e200: bool = True, below_e200: bool = False) -> tuple:
    """
    V3 Trend Pullback with regime-aware RSI zones and macro trend filtering (EMA200).
    Max theoretical: ~7.5 points per direction.
    """
    long_score = 0.0
    short_score = 0.0

    if adx_val > 50:
        rsi_min_long, rsi_max_long = 45, 65
        rsi_min_short, rsi_max_short = 55, 75
    else:
        rsi_min_long, rsi_max_long = 35, 55
        rsi_min_short, rsi_max_short = 45, 65

    # LONG: Bull alignment + MACD bullish (strong signal)
    # OR just MACD bullish (weaker but still valid)
    # Add bonus if RSI is in ideal zone, penalty if overbought
    if bull_align and macd_line > signal_line:
        long_score += 4.0  # Strong: EMA + MACD aligned
        if rsi_min_long <= rsi_val <= rsi_max_long:
            long_score += 1.5  # Bonus: RSI in sweet spot
        elif rsi_val < rsi_min_long:
            long_score += 1.0  # Bonus: RSI oversold (strong signal)
        elif rsi_val > rsi_max_long:
            long_score -= 0.5  # Penalty: RSI overbought (risky)
    elif macd_line > signal_line:
        # MACD momentum only (no EMA alignment) — weaker base
        long_score += 2.5
        if rsi_val < rsi_min_long:
            long_score += 0.75  # Bonus: RSI oversold helps
        elif rsi_val > rsi_max_long:
            long_score -= 0.25  # Penalty: overbought still risky

    # SHORT: Bear alignment + MACD bearish (strong signal)
    # OR just MACD bearish (weaker but still valid)
    # Add bonus if RSI is in ideal zone, penalty if oversold
    if bear_align and macd_line < signal_line:
        short_score += 4.0  # Strong: EMA + MACD aligned
        if rsi_min_short <= rsi_val <= rsi_max_short:
            short_score += 1.5  # Bonus: RSI in sweet spot
        elif rsi_val > rsi_max_short:
            short_score += 1.0  # Bonus: RSI overbought (strong signal)
        elif rsi_val < rsi_min_short:
            short_score -= 0.5  # Penalty: RSI oversold (risky)
    elif macd_line < signal_line:
        # MACD momentum only (no EMA alignment) — weaker base
        short_score += 2.5
        if rsi_val > rsi_max_short:
            short_score += 0.75  # Bonus: RSI overbought helps
        elif rsi_val < rsi_min_short:
            short_score -= 0.25  # Penalty: oversold still risky

    # MACD histogram confirmation
    if hist_curr > 0 and hist_curr > hist_prev:
        long_score += 1.5
    if hist_curr < 0 and hist_curr < hist_prev:
        short_score += 1.5

    # Volume confirmation — only boost the leading direction
    vol_r = volume_ratio(volumes)
    if vol_r > 1.3:
        vol_bonus = 1.0
    elif vol_r > 1.1:
        vol_bonus = 0.5
    else:
        vol_bonus = 0.0

    if vol_bonus > 0:
        if long_score >= short_score:
            long_score += vol_bonus
        else:
            short_score += vol_bonus

    # ── Macro trend filter (EMA200): Soft penalty for counter-trend trades ──
    if long_score > 0 and not above_e200:
        long_score -= 1.5  # Penalty: LONG against macro downtrend
    if short_score > 0 and not below_e200:
        short_score -= 1.5  # Penalty: SHORT against macro uptrend

    return long_score, short_score



# ─────────────────────────────────────────────────────────────────
# STRATEGY 3: BREAKOUT (new — for squeeze/compression release)
# Best when: Bollinger squeeze + volume explosion
# ─────────────────────────────────────────────────────────────────
def score_breakout(closes: list, candles: list, rsi_val: float,
                   macd_line: float, signal_line: float,
                   hist_curr: float, hist_prev: float,
                   vol_r: float, adx_val: float,
                   bull_align: bool, bear_align: bool,
                   above_e200: bool = True, below_e200: bool = False) -> tuple:
    """
    Breakout: catch the initial move out of a compression range.
    Uses Bollinger squeeze, volume surge, and directional confirmation.
    Includes macro trend filtering (EMA200) for soft penalty on counter-trend breakouts.
    Max theoretical: ~8.0 points per direction.
    """
    long_score = 0.0
    short_score = 0.0

    mid_bb, upper_bb, lower_bb = bollinger(closes)
    price = closes[-1]
    prev_price = closes[-2] if len(closes) > 1 else price

    # 1. Squeeze release direction (0-3 pts)
    if price > upper_bb and prev_price <= upper_bb:
        long_score += 3.0    # breaking above upper band
    elif price > upper_bb:
        long_score += 1.5    # already above, confirming

    if price < lower_bb and prev_price >= lower_bb:
        short_score += 3.0   # breaking below lower band
    elif price < lower_bb:
        short_score += 1.5

    # 2. Volume surge (0-2 pts)
    if vol_r > 2.0:
        long_score += 2.0; short_score += 2.0
    elif vol_r > 1.5:
        long_score += 1.5; short_score += 1.5
    elif vol_r > 1.3:
        long_score += 0.5; short_score += 0.5

    # 3. ADX rising (trend forming) (0-1 pt)
    # We can't directly compute ADX delta here, but ADX > 20 is positive
    if adx_val > 20:
        long_score += 0.5; short_score += 0.5
    if adx_val > 15:
        long_score += 0.5; short_score += 0.5

    # 4. MACD confirmation (0-1 pt)
    if macd_line > signal_line and hist_curr > 0:
        long_score += 1.0
    if macd_line < signal_line and hist_curr < 0:
        short_score += 1.0

    # 5. Market structure confirmation (0-1 pt)
    hh, ll = market_structure(candles)
    if hh:
        long_score += 1.0
    if ll:
        short_score += 1.0

    # Volume only boosts the dominant direction
    vol_portion = min(long_score, short_score)
    if long_score > short_score:
        short_score -= vol_portion * 0.3
    elif short_score > long_score:
        long_score -= vol_portion * 0.3

    # ── Macro trend filter (EMA200): Soft penalty for counter-trend breakouts ──
    if long_score > 0 and not above_e200:
        long_score -= 1.5  # Penalty: breakout LONG against macro downtrend
    if short_score > 0 and not below_e200:
        short_score -= 1.5  # Penalty: breakout SHORT against macro uptrend

    return max(0, long_score), max(0, short_score)


# ─────────────────────────────────────────────────────────────────
# ATR-BASED TP/SL — Single implementation (no more trade_setup duplicate)
# ─────────────────────────────────────────────────────────────────
def suggest_tp_sl(candles_4h: list, direction: str,
                  multiplier_sl: float = 1.5, rr_ratio: float = 2.0) -> dict:
    """
    Compute TP/SL levels from ATR and risk/reward ratio.

    OHLCV indexing: [0=open, 1=high, 2=low, 3=close, 4=volume]

    Returns dict with entry_price, suggested_tp, suggested_sl, sl_pct, tp_pct, atr.
    """
    closes = [float(c[3]) for c in candles_4h]
    highs  = [float(c[1]) for c in candles_4h]
    lows   = [float(c[2]) for c in candles_4h]

    entry_price = closes[-1]

    # ATR (20-period)
    tr_values = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_values.append(tr)

    atr = sum(tr_values[-20:]) / min(20, len(tr_values))

    sl_distance = atr * multiplier_sl
    tp_distance = sl_distance * rr_ratio

    if direction == "LONG":
        suggested_sl = entry_price - sl_distance
        suggested_tp = entry_price + tp_distance
    else:
        suggested_sl = entry_price + sl_distance
        suggested_tp = entry_price - tp_distance

    sl_pct = (sl_distance / entry_price) * 100
    tp_pct = (tp_distance / entry_price) * 100

    return {
        "entry_price": round(entry_price, 8),
        "suggested_sl": round(suggested_sl, 8),
        "suggested_tp": round(suggested_tp, 8),
        "sl_pct": round(sl_pct, 2),
        "tp_pct": round(tp_pct, 2),
        "atr": round(atr, 8),
        "rr_ratio": rr_ratio,
    }


def score_technical(symbol: str, candles_4h: list, precomputed_indicators: dict = None) -> dict:
    """
    Multi-regime technical scoring.
    Detects market regime (trending/ranging/breakout) and applies
    the appropriate strategy. Returns the best signal from the active regime.

    Args:
        symbol: Trading pair
        candles_4h: OHLCV candles
        precomputed_indicators: Optional dict with pre-computed indicator values (optimization for backtester).
                               If provided, uses these instead of computing.
    """
    closes  = [c[3] for c in candles_4h]
    volumes = [c[4] for c in candles_4h]
    highs   = [c[1] for c in candles_4h]
    lows    = [c[2] for c in candles_4h]

    if len(closes) < 50:
        return {"score": 0, "direction": "NEUTRAL", "long_score": 0, "short_score": 0, "details": {}}

    # ── Use pre-computed indicators if provided (optimization), else compute ──
    if precomputed_indicators:
        # Use pre-computed cheap indicators (RSI, MACD, EMA)
        rsi_val = precomputed_indicators.get('rsi', 50.0)
        macd_line_val = precomputed_indicators.get('macd_line', 0.0)
        sig_line_val = precomputed_indicators.get('macd_signal', 0.0)
        hist_curr = precomputed_indicators.get('macd_hist', 0.0)
        hist_prev = precomputed_indicators.get('macd_hist_prev', 0.0)
        e9 = precomputed_indicators.get('e9')
        e21 = precomputed_indicators.get('e21')
        e50 = precomputed_indicators.get('e50')
        bull_align = precomputed_indicators.get('bull_align', False)
        bear_align = precomputed_indicators.get('bear_align', False)
        e200 = precomputed_indicators.get('e200')
        above_e200 = precomputed_indicators.get('above_e200', True)
        below_e200 = precomputed_indicators.get('below_e200', False)

        # Compute expensive indicators on-demand (ADX, Bollinger only computed here)
        adx_val = adx(highs, lows, closes, period=14)
        vol_r = volume_ratio(volumes)
        bb_bw, bb_squeeze = bollinger_bandwidth(closes)
    else:
        # Compute all indicators once (slower path, for live scanner)
        rsi_val = rsi(closes)
        macd_line_val, sig_line_val, hist_curr = macd(closes)
        hist_prev = macd(closes[:-1])[2] if len(closes) > 27 else hist_curr
        vol_r = volume_ratio(volumes)
        e9, e21, e50, bull_align, bear_align = ema_alignment(closes)
        adx_val = adx(highs, lows, closes, period=14)

        # 200-EMA macro trend filter (migrated to 1H: use 800 to maintain ~33-day lookback)
        # 4H system: 200 × 4h = 800 hours ≈ 33 days
        # 1H system: 800 × 1h = 800 hours ≈ 33 days (equivalent scale)
        e200 = ema(closes, min(800, len(closes)))[-1]
        above_e200 = closes[-1] > e200
        below_e200 = closes[-1] < e200

        # Bollinger bandwidth & squeeze detection
        bb_bw, bb_squeeze = bollinger_bandwidth(closes)

    # ── Detect market regime ──
    regime = detect_regime(adx_val, bb_squeeze, vol_r, closes)

    # Debug: Log regime and key indicators
    dbg.debug(f"[{symbol}] Regime={regime} | ADX={adx_val:.1f} BB_squeeze={bb_squeeze} vol_r={vol_r:.2f}")
    dbg.debug(f"[{symbol}] Indicators | RSI={rsi_val:.1f} MACD={macd_line_val:.6f} sig={sig_line_val:.6f} hist={hist_curr:.6f} prev={hist_prev:.6f}")
    dbg.debug(f"[{symbol}] EMA align | bull={bull_align} bear={bear_align} above_e200={above_e200} e200={e200:.2f}")

    # ── Score using regime-appropriate strategy ──
    if regime == "trending":
        long_pts, short_pts = score_trend_pullback(
            closes, volumes, rsi_val, macd_line_val, sig_line_val,
            hist_curr, hist_prev, bull_align, bear_align, adx_val,
            above_e200=above_e200, below_e200=below_e200
        )
        strategy = "trend_pullback"

    elif regime == "weak_trend":
        # Use trend pullback with softened ADX penalty (mean reversion disabled)
        long_pts, short_pts = score_trend_pullback(
            closes, volumes, rsi_val, macd_line_val, sig_line_val,
            hist_curr, hist_prev, bull_align, bear_align, adx_val,
            above_e200=above_e200, below_e200=below_e200
        )

        # Gradient ADX penalty for weak trend (0.5-0.8x in ADX 20-25 range)
        # Floor at 0.3 to prevent negative multipliers when ADX < ~16.7
        adx_mult = max(0.3, 0.5 + (adx_val - 20) / 5 * 0.3)
        long_pts *= adx_mult
        short_pts *= adx_mult
        dbg.debug(f"[{symbol}] ADX mult={adx_mult:.2f} → LONG={long_pts:.2f} SHORT={short_pts:.2f}")

        strategy = "trend_pullback_weak"

    elif regime == "breakout":
        long_pts, short_pts = score_breakout(
            closes, candles_4h, rsi_val, macd_line_val, sig_line_val,
            hist_curr, hist_prev, vol_r, adx_val, bull_align, bear_align,
            above_e200=above_e200, below_e200=below_e200
        )
        strategy = "breakout"

    # NOTE: Mean reversion regime removed (was losing -$475 in 12mo backtest)
    # All low-ADX conditions now use weak_trend (trend pullback logic)

    long_pts  = round(long_pts,  2)
    short_pts = round(short_pts, 2)
    dbg.debug(f"[{symbol}] Raw scores | LONG={long_pts:.2f} SHORT={short_pts:.2f}")

    details = {
        "rsi":         {"1h": round(rsi_val, 1)},
        "macd_line":   round(macd_line_val, 6),
        "signal_line": round(sig_line_val, 6),
        "hist_curr":   round(hist_curr, 6),
        "hist_prev":   round(hist_prev, 6),
        "vol_ratio":   round(vol_r, 2),
        "adx":         round(adx_val, 1),
        "ema200":      round(e200, 4),
        "above_e200":  above_e200,
        "ema_bull":    bull_align,
        "ema_bear":    bear_align,
        "regime":      regime,
        "strategy":    strategy,
        "bb_bandwidth": bb_bw,
        "bb_squeeze":  bb_squeeze,
        "scoring":     f"multi_regime_{strategy}",
    }

    if long_pts >= short_pts:
        return {"score": long_pts, "direction": "LONG",  "long_score": long_pts, "short_score": short_pts, "details": details}
    else:
        return {"score": short_pts, "direction": "SHORT", "long_score": long_pts, "short_score": short_pts, "details": details}


# ─────────────────────────────────────────────────────────────────
# FUNDAMENTALS  (0-5 points)
# ─────────────────────────────────────────────────────────────────
_fundamental_cache = {"ts": 0, "data": {}}

def fetch_fundamentals(direction: str, symbol: str = "BTCUSDT") -> dict:
    """Fetch fundamentals for a specific symbol.

    Fear & Greed: Global (applies to all)
    BTC Dominance: Global (applies to all)
    Funding Rate: PER-SYMBOL
    Long/Short Ratio: PER-SYMBOL
    """
    global _fundamental_cache
    now = time.time()
    cache_key = f"{symbol}_fund"
    if cache_key not in _fundamental_cache:
        _fundamental_cache[cache_key] = {"ts": 0, "data": {}}

    cache_entry = _fundamental_cache[cache_key]
    if now - cache_entry["ts"] < 30:
        return cache_entry["data"]

    result = {}
    score  = 0

    # Fear & Greed (Global)
    fg = get("https://api.alternative.me/fng/?limit=1")
    if fg and fg.get("data"):
        fgi = int(fg["data"][0]["value"])
        result["fear_greed"] = fgi
        if fgi < 25:
            result["fg_signal"] = "extreme_fear_bull"
            score += 1.5
        elif fgi > 75:
            result["fg_signal"] = "extreme_greed_bear"
            score -= 1.5
        elif fgi < 40:
            score += 0.5
        elif fgi > 60:
            score -= 0.5
    result["fg_score"] = round(score, 2)
    fg_score_val = score
    score = 0

    # BTC Dominance (Global)
    cg = get("https://api.coingecko.com/api/v3/global")
    if cg and cg.get("data"):
        btc_dom = cg["data"].get("market_cap_percentage", {}).get("btc", 50)
        result["btc_dominance"] = round(btc_dom, 2)
        result["btc_dom_bias"] = "btc_bull" if btc_dom > 55 else "alt_bull"

    # Funding Rate (PER-SYMBOL)
    funding_data = get("https://fapi.binance.com/fapi/v1/premiumIndex")
    if funding_data and isinstance(funding_data, list):
        symbol_fund = next((float(x["lastFundingRate"]) for x in funding_data
                           if x.get("symbol") == symbol), None)
        if symbol_fund is not None:
            result["funding_rate"] = round(symbol_fund * 100, 4)
            if symbol_fund > 0.001:
                score -= 1
                result["funding_signal"] = "high_longs_squeeze_risk"
            elif symbol_fund < -0.001:
                score += 1
                result["funding_signal"] = "negative_short_squeeze_risk"
    result["funding_score"] = round(score, 2)
    funding_score_val = score
    score = 0

    # Open Interest (per-symbol)
    oi_data = get("https://fapi.binance.com/fapi/v1/openInterest", {"symbol": symbol})
    if oi_data:
        result["open_interest"] = oi_data.get("openInterest")
    result["oi_score"] = 0

    # Long/Short Ratio (PER-SYMBOL)
    ls = get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
             {"symbol": symbol, "period": "5m", "limit": 2})
    if ls and len(ls) >= 2:
        ratio_now  = float(ls[-1]["longShortRatio"])
        ratio_prev = float(ls[-2]["longShortRatio"])
        result["ls_ratio"] = round(ratio_now, 3)
        if ratio_now > 2.0:
            score -= 0.5
            result["ls_signal"] = "crowded_longs"
        elif ratio_now < 0.5:
            score += 0.5
            result["ls_signal"] = "crowded_shorts"
        if ratio_now > ratio_prev:
            score += 0.25
        else:
            score -= 0.25
    result["ls_score"] = round(score, 2)

    total = fg_score_val + funding_score_val + score
    total = round(total, 2)
    result["total_raw"] = total
    result["direction_score"] = total

    cache_entry["ts"] = now
    cache_entry["data"] = result
    return result


def fundamental_score(direction: str, symbol: str = "BTCUSDT") -> tuple:
    """Returns (long_pts: 0-5, short_pts: 0-5, details_dict)"""
    f = fetch_fundamentals(direction, symbol)
    raw = f.get("direction_score", 0)
    if raw > 0:
        long_pts  = min(5, raw)
        short_pts = 0.0
    elif raw < 0:
        long_pts  = 0.0
        short_pts = min(5, -raw)
    else:
        long_pts  = 0.0
        short_pts = 0.0
    return round(long_pts, 2), round(short_pts, 2), f


# ─────────────────────────────────────────────────────────────────
# NEWS SENTIMENT  (0-3 points)
# ─────────────────────────────────────────────────────────────────
BULL_PHRASES = {
    "bullish breakout", "bull run", "bull market", "buying pressure",
    "accumulation", "strong demand", "institutional buying", "inflow",
    "bullish signal", "strong support", "new high", "new record",
    "record high", "all-time high", "breakout above"
}
BEAR_PHRASES = {
    "bear market", "selling pressure", "bearish signal", "weak support",
    "support broken", "resistance break", "breakdown", "downtrend",
    "lower highs", "lower lows", "selling off", "institutional selling",
    "outflow", "panic selling"
}
BULL_WORDS = {
    "bullish", "surge", "rally", "breakout", "pump", "spike", "boom",
    "rocket", "soar", "moon", "jump", "gains", "bull",
    "buy", "adoption", "upgrade", "launch", "partnership", "approval",
    "endorsement", "success", "positive", "growth", "recovery", "outperform",
    "beat", "milestone", "record", "exceed", "ath",
    "institutional", "inflow", "support"
}
BEAR_WORDS = {
    "bearish", "crash", "dump", "plunge", "collapse", "tank", "tumble",
    "slump", "bear", "decline", "drop", "negative", "failure", "fail",
    "miss", "trouble", "scandal", "violation", "falter", "weakness",
    "underperform", "outflow", "breakdown", "downtrend"
}

SYMBOL_KEYWORDS = {
    "BTCUSDT": ["bitcoin", "btc", "satoshi", "blockchain"],
    "ETHUSDT": ["ethereum", "eth", "solidity", "smart contract"],
    "SOLUSDT": ["solana", "sol"],
    "XRPUSDT": ["ripple", "xrp"],
    "BNBUSDT": ["binance", "bnb"],
    "DOGEUSDT": ["dogecoin", "doge"],
    "ADAUSDT": ["cardano", "ada"],
    "AVAXUSDT": ["avalanche", "avax"],
    "LINKUSDT": ["chainlink", "link"],
    "DOTUSDT": ["polkadot", "dot"],
}

_news_cache = {"ts": 0, "by_symbol": {}}


def filter_headlines_by_symbol(headlines: list, symbol: str) -> list:
    """Filter headlines to only include those relevant to the symbol."""
    if symbol not in SYMBOL_KEYWORDS:
        return headlines

    keywords = SYMBOL_KEYWORDS[symbol]
    filtered = [h for h in headlines if any(kw in h.lower() for kw in keywords)]

    # Return only matched headlines. If no matches, return empty (no news = no score)
    # This prevents generic headlines from inflating scores for symbols with no relevant news
    return filtered


def fetch_rss_headlines(url: str, limit: int = 15) -> list:
    """Fetch and parse RSS feed. Returns list of headlines."""
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 (CryptoScanner)"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        headlines = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            if title:
                headlines.append(title)
        if not headlines:
            for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
                title = entry.findtext("{http://www.w3.org/2005/Atom}title", "")
                if title:
                    headlines.append(title)
        return headlines[:limit]
    except Exception as e:
        log.debug(f"RSS fetch failed ({url}): {type(e).__name__}")
        return []


def fetch_news_sentiment(symbol: str = "BTCUSDT") -> tuple:
    """Fetch and score news sentiment specific to the given symbol."""
    global _news_cache
    now = time.time()

    if symbol not in _news_cache["by_symbol"]:
        _news_cache["by_symbol"][symbol] = {"ts": 0, "score": 0.0, "details": {}}

    cache_entry = _news_cache["by_symbol"][symbol]
    if now - cache_entry["ts"] < 60:
        return cache_entry["score"], cache_entry["details"]

    headlines = []
    source_used = None

    rss_sources = [
        ("CoinTelegraph", "https://feeds.cointelegraph.com/feed/news"),
        ("CoinDesk", "https://feeds.coindesk.com/news"),
        ("Kraken Blog", "https://blog.kraken.com/feed/"),
        ("TheBlock", "https://feeds.theblockcrypto.com/feed"),
    ]

    for source_name, rss_url in rss_sources:
        headlines = fetch_rss_headlines(rss_url, limit=15)
        if headlines:
            source_used = source_name
            log.debug(f"News: Fetched {len(headlines)} headlines from {source_name}")
            break
        log.debug(f"News: {source_name} unavailable, trying next source...")

    filtered_headlines = filter_headlines_by_symbol(headlines, symbol)

    if not filtered_headlines:
        details = {"headlines": 0, "bull_signals": 0, "bear_signals": 0,
                   "sentiment": "neutral", "source": source_used}
        dbg.debug(f"[{symbol}] News: No headlines matching symbol (0 filtered from {len(headlines)} total)")
        cache_entry.update({"ts": now, "score": 0.0, "details": details})
        return 0.0, details

    # Debug: Log filtered headlines
    dbg.debug(f"[{symbol}] News: {len(filtered_headlines)} headlines matching symbol from {source_used}:")
    for i, h in enumerate(filtered_headlines, 1):
        dbg.debug(f"  [{i}] {h}")

    bull_score = bear_score = 0.0
    for h in filtered_headlines:
        h_lower = h.lower()
        for phrase in BULL_PHRASES:
            if phrase in h_lower:
                bull_score += h_lower.count(phrase) * 2.0
        for phrase in BEAR_PHRASES:
            if phrase in h_lower:
                bear_score += h_lower.count(phrase) * 2.0
        for word in BULL_WORDS:
            if word in h_lower:
                bull_score += h_lower.count(word) * 1.0
        for word in BEAR_WORDS:
            if word in h_lower:
                bear_score += h_lower.count(word) * 1.0

    total = bull_score + bear_score
    bull_count = int(bull_score)
    bear_count = int(bear_score)

    # Debug: Log raw scores
    dbg.debug(f"[{symbol}] News scoring: bull_score={bull_score:.1f} bear_score={bear_score:.1f} total={total:.1f}")

    if total == 0:
        score = 0.0
        sentiment = "neutral"
    else:
        ratio = bull_count / total
        dbg.debug(f"[{symbol}] Bull/Bear ratio: {bull_count}/{total} = {ratio:.2f}")

        if ratio >= 0.70:
            score = 2.5 + (ratio - 0.70) * 10
            sentiment = "strong_bullish"
        elif ratio >= 0.60:
            score = 1.5 + (ratio - 0.60) * 10
            sentiment = "bullish"
        elif ratio >= 0.55:
            score = 0.5 + (ratio - 0.55) * 10
            sentiment = "slightly_bullish"
        elif ratio <= 0.30:
            score = 2.5 + ((1 - ratio) - 0.70) * 10
            sentiment = "strong_bearish"
        elif ratio <= 0.40:
            score = 1.5 + ((1 - ratio) - 0.60) * 10
            sentiment = "bearish"
        elif ratio <= 0.45:
            score = 0.5 + ((1 - ratio) - 0.55) * 10
            sentiment = "slightly_bearish"
        else:
            score = 0.0
            sentiment = "neutral"

        score = min(3.0, score)
        dbg.debug(f"[{symbol}] News sentiment: {sentiment} (score={score:.2f})")

    details = {
        "headlines": len(filtered_headlines),
        "bull_signals": bull_count,
        "bear_signals": bear_count,
        "sentiment": sentiment,
        "source": source_used,
    }

    cache_entry.update({"ts": now, "score": round(score, 2), "details": details})
    return round(score, 2), details


# ─────────────────────────────────────────────────────────────────
# GENERATE SIGNAL — THE SINGLE SOURCE OF TRUTH
#
# Both live scanner and backtester call this.
# Returns a complete, self-contained trade signal with TP/SL.
# ─────────────────────────────────────────────────────────────────
def generate_signal(symbol: str, candles_4h: list,
                    include_fundamentals: bool = True,
                    include_news: bool = True,
                    state: dict = None,
                    current_time: Optional[datetime] = None,
                    precomputed_indicators: dict = None) -> Optional[dict]:
    """
    Generate a complete trade signal from candle data.

    Args:
        symbol: Trading pair (e.g. "BTCUSDT")
        candles_4h: OHLCV candles [[open, high, low, close, volume], ...]
        include_fundamentals: False for backtesting (no historical data available)
        include_news: False for backtesting (no historical data available)
        current_time: Time to use for session filter (defaults to now). Backtester passes candle timestamp
        precomputed_indicators: Optional dict of pre-computed indicators (optimization for backtester)

    Returns:
        Signal dict or None if no signal. Signal contains everything needed
        to execute the trade — the backtester just simulates fills against
        the tp/sl this function returns.
    """
    tech = score_technical(symbol, candles_4h, precomputed_indicators=precomputed_indicators)
    if tech["direction"] == "NEUTRAL":
        return None

    tech_long  = tech["long_score"]
    tech_short = tech["short_score"]

    # Fundamentals (skip in backtest — historical data unavailable)
    fund_long = fund_short = 0.0
    fund_details = {}
    if include_fundamentals:
        fund_long, fund_short, fund_details = fundamental_score("LONG", symbol)

    # News (skip in backtest — historical headlines unavailable)
    news_long = news_short = 0.0
    news_details = {}
    if include_news:
        news_score_val, news_details = fetch_news_sentiment(symbol)
        sentiment = news_details.get("sentiment", "neutral")

        # Handle all bullish sentiments: slightly_bullish, bullish, strong_bullish
        if "bullish" in sentiment:
            news_long, news_short = news_score_val, 0.0
        # Handle all bearish sentiments: slightly_bearish, bearish, strong_bearish
        elif "bearish" in sentiment:
            news_long, news_short = 0.0, news_score_val
        # Neutral sentiment: no score contribution
        else:
            news_long, news_short = 0.0, 0.0

    # Total scores per direction
    long_total  = round(tech_long  + fund_long  + news_long,  2)
    short_total = round(tech_short + fund_short + news_short, 2)

    # Debug: Log score breakdown
    dbg.debug(f"[{symbol}] Score breakdown | Tech L={tech_long:.2f} S={tech_short:.2f} | Fund L={fund_long:.2f} S={fund_short:.2f} | News L={news_long:.2f} S={news_short:.2f}")
    dbg.debug(f"[{symbol}] Totals | LONG={long_total:.2f} SHORT={short_total:.2f}")

    # Reject weak signals (both sides too weak)
    if long_total < 1.0 and short_total < 1.0:
        # Debug: Log why signal was rejected (helps detect fund/news issues)
        if tech_long > 1.0 or tech_short > 1.0:
            # Tech had a signal but fund/news killed it
            dbg.debug(f"[{symbol}] REJECTED: both totals < 1.0 | tech was L={tech_long:.1f} S={tech_short:.1f}")
            _last_rejection_reason[symbol] = f"Weak (L={long_total:.1f} S={short_total:.1f})"
        else:
            _last_rejection_reason[symbol] = "No Signal"
        return None

    # Reject ambiguous signals: require minimum gap between long and short scores
    # If both directions score similarly, the signal has no clear conviction
    # Lowered from 1.5 to 1.0: testing shows more low-conviction trades are profitable
    MIN_SCORE_DIFFERENTIAL = 1.0
    score_gap = abs(long_total - short_total)
    if score_gap < MIN_SCORE_DIFFERENTIAL:
        dbg.debug(f"[{symbol}] REJECTED: ambiguous signal | L={long_total:.2f} S={short_total:.2f} gap={score_gap:.2f} < {MIN_SCORE_DIFFERENTIAL}")
        _last_rejection_reason[symbol] = f"Ambiguous (L={long_total:.1f} S={short_total:.1f})"
        return None

    if long_total >= short_total:
        direction      = "LONG"
        total_score    = long_total
        tech_component = tech_long
        fund_component = fund_long
        news_component = news_long
    else:
        direction      = "SHORT"
        total_score    = short_total
        tech_component = tech_short
        fund_component = fund_short
        news_component = news_short

    # Whipsaw detection: reject if direction flipped within last 5 minutes
    if state and is_whipsaw(state, symbol, direction):
        dbg.debug(f"[{symbol}] REJECTED: whipsaw detected (direction change <5min)")
        _last_rejection_reason[symbol] = "Whipsaw"
        return None

    # TP/SL from ATR — strategy-specific parameters
    strategy = tech["details"].get("strategy", "trend_pullback")
    regime   = tech["details"].get("regime", "trending")

    if strategy == "mean_reversion":
        # Tighter stops, smaller targets — trading back to the mean
        tp_sl = suggest_tp_sl(candles_4h, direction, multiplier_sl=1.0, rr_ratio=1.5)
    elif strategy == "breakout":
        # Wider stops, bigger targets — catching the expansion move
        tp_sl = suggest_tp_sl(candles_4h, direction, multiplier_sl=2.0, rr_ratio=2.5)
    else:
        # Trend pullback (default): balanced
        tp_sl = suggest_tp_sl(candles_4h, direction, multiplier_sl=1.5, rr_ratio=2.0)

    # ─────────────────────────────────────────────────────────────────
    # SIGNAL QUALITY FILTERS — Skip low-conviction combos
    # ─────────────────────────────────────────────────────────────────
    rsi = tech["details"].get("rsi", {}).get("1h", 50)
    adx = tech["details"].get("adx", 25)
    time_for_filter = current_time if current_time is not None else datetime.now(timezone.utc)
    hour_utc = time_for_filter.hour

    # Filter 1: SHORT trend_pullback only at RSI 40-50 (market mid-range, room to run)
    # Note: exact match — does NOT apply to trend_pullback_weak (low-ADX regime)
    if direction == "SHORT" and strategy == "trend_pullback":
        if not (40 <= rsi < 50):
            dbg.debug(f"[{symbol}] FILTERED: SHORT trend_pullback at RSI {rsi:.0f} (only RSI 40-50 allowed)")
            _last_rejection_reason[symbol] = f"RSI filter (RSI={rsi:.0f})"
            return None

    # Filter 2: LONG breakout — skip entirely (losing pattern)
    if direction == "LONG" and strategy == "breakout":
        dbg.debug(f"[{symbol}] FILTERED: LONG breakout (inherently unprofitable)")
        _last_rejection_reason[symbol] = "LONG breakout"
        return None

    # Filter 3: LONG trend_pullback at RSI 60-70 — only if ADX 40+ (parabolic trend)
    # Note: exact match — does NOT apply to trend_pullback_weak (low-ADX regime)
    if direction == "LONG" and strategy == "trend_pullback":
        if 60 <= rsi < 70 and adx < 40:
            dbg.debug(f"[{symbol}] FILTERED: LONG trend_pullback RSI {rsi:.0f} ADX {adx:.1f} (need ADX 40+ at RSI 60-70)")
            _last_rejection_reason[symbol] = f"ADX filter (ADX={adx:.0f})"
            return None

    # Filter 4: Asia session (0-8 UTC) — low liquidity, false signals
    # NOTE: current_time parameter allows backtester to pass candle timestamp
    # Live scanner uses current time (via default), which is correct for real-time alerts
    if hour_utc >= 0 and hour_utc < 8:
        dbg.debug(f"[{symbol}] FILTERED: Asia session hour {hour_utc} UTC (low liquidity)")
        _last_rejection_reason[symbol] = f"Asia session (UTC {hour_utc})"
        return None

    return {
        "symbol":             symbol,
        "direction":          direction,
        "score":              total_score,
        "entry_price":        tp_sl["entry_price"],
        "tp":                 tp_sl["suggested_tp"],
        "sl":                 tp_sl["suggested_sl"],
        "tp_pct":             tp_sl["tp_pct"],
        "sl_pct":             tp_sl["sl_pct"],
        "atr":                tp_sl["atr"],
        "rr_ratio":           tp_sl["rr_ratio"],
        "technical_score":    round(tech_component, 2),
        "fundamental_score":  round(fund_component, 2),
        "news_score":         round(news_component, 2),
        "long_score":         long_total,
        "short_score":        short_total,
        "regime":             regime,
        "strategy":           strategy,
        "details":            tech["details"],
        "fund_details":       fund_details,
        "news_details":       news_details,
    }


# ─────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram credentials not set, skipping send.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram message sent.")
    except Exception as e:
        log.error(f"Telegram error: {e}")


def format_alert(signal: dict) -> str:
    """Format a complete signal into a Telegram alert message."""
    direction = signal["direction"]
    symbol    = signal["symbol"]
    coin      = symbol.replace("USDT", "")
    price     = signal["entry_price"]
    total     = signal["score"]
    tech_pts  = signal["technical_score"]
    fund_pts  = signal["fundamental_score"]
    news_pts  = signal["news_score"]
    details   = signal["details"]
    fund_det  = signal.get("fund_details", {})
    news_det  = signal.get("news_details", {})

    emoji = "🟢🚀" if direction == "LONG" else "🔴📉"

    rsi_str = " | ".join(f"{tf}: {v:.0f}" for tf, v in details.get("rsi", {}).items())
    vol_r = details.get("vol_ratio", "N/A")
    adx_val = details.get("adx", "N/A")

    conf_bar = "█" * int(total) + "░" * (20 - int(total))

    if total >= ALERT_THRESHOLD_HARD:
        conf_label = "HIGH CONFIDENCE ✅"
    elif total >= ALERT_THRESHOLD_OPTB:
        conf_label = "ENTRY ✅"
    else:
        conf_label = "WATCH"

    # Strategy display names
    strategy_names = {
        "trend_pullback": "📈 Trend Pullback",
        "trend_pullback_weak": "📈 Trend (Forming)",
        "mean_reversion": "🔄 Mean Reversion",
        "breakout": "💥 Breakout",
    }
    regime_names = {
        "trending": "Trending",
        "ranging": "Ranging",
        "breakout": "Breakout",
        "weak_trend": "Weak Trend",
    }
    strategy_label = strategy_names.get(signal.get("strategy", ""), "📊 Technical")
    regime_label = regime_names.get(signal.get("regime", ""), "Unknown")

    msg = (
        f"{emoji} <b>{direction} {coin}</b> @ <code>${price:,.4f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Score: {total:.1f}/20</b>  {conf_label}\n"
        f"<code>[{conf_bar}]</code>\n"
        f"{strategy_label} | Regime: {regime_label}\n\n"
        f"📊 <b>Technical</b>  ({tech_pts:.1f}/~8)\n"
        f"  RSI: {rsi_str} | ADX: {adx_val}\n"
        f"  Vol Ratio: {vol_r}\n\n"
        f"🌐 <b>Fundamentals</b>  ({fund_pts:.1f}/5)\n"
        f"  Fear&Greed: {fund_det.get('fear_greed', 'N/A')} ({fund_det.get('fg_signal', 'neutral')})\n"
        f"  Funding: {fund_det.get('funding_rate', 'N/A')}% ({fund_det.get('funding_signal', 'normal')})\n"
        f"  L/S Ratio: {fund_det.get('ls_ratio', 'N/A')} ({fund_det.get('ls_signal', 'normal')})\n\n"
        f"📰 <b>News</b>  ({news_pts:.1f}/3)\n"
        f"  {news_det.get('sentiment','neutral').upper()} | "
        f"🐂{news_det.get('bull_signals',0)} 🐻{news_det.get('bear_signals',0)} | "
        f"📰 {news_det.get('source', 'N/A')}\n\n"
        f"📐 <b>Trade Setup (ATR-based, {signal['rr_ratio']:.1f}:1 R:R)</b>\n"
        f"  Entry:  ${price:,.4f}\n"
        f"  SL:     ${signal['sl']:,.4f}  ({-signal['sl_pct']:+.2f}%)\n"
        f"  TP:     ${signal['tp']:,.4f}  ({signal['tp_pct']:+.2f}%)\n"
        f"  ATR:    {signal['atr']:,.8f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    )
    return msg


# ─────────────────────────────────────────────────────────────────
# MAIN SCAN LOOP — Uses generate_signal() as the single path
# ─────────────────────────────────────────────────────────────────

# Module-level cycle results (replaces fragile function attribute hack)
_cycle_results = []
_last_rejection_reason = {}  # Track rejection reasons for each symbol


def scan_symbol(symbol: str, state: dict):
    """Scan a single symbol using generate_signal() as single source of truth."""
    dbg.debug(f"[{symbol}] Scanning…")

    # Fetch 4001 1h candles — extra 1 because we drop the forming candle below.
    # The last candle from Binance is always the CURRENT incomplete candle.
    # Its volume, high, low, and close are all partial — using it produces signals
    # that don't match the backtester which always works on fully closed candles.
    # NOTE: Live scanning uses FRESH data only (no cache) to avoid stale price data.
    # Caching is for backtesting only where determinism matters. Live trading needs current prices.
    candles_1h = fetch_klines_cached(symbol, "1h", 4001, use_cache=False)
    if not candles_1h:
        log.warning(f"  No 1h data for {symbol}, skipping.")
        return

    # Drop the forming (incomplete) candle — use only fully closed candles.
    # This is the WYTIWYT guarantee: identical data quality to the backtester.
    # The scanner runs every 5 minutes, so we catch each new closed candle
    # within 5 minutes of it closing — no meaningful delay in signal detection.
    completed_candles = candles_1h[:-1]

    if len(completed_candles) < 1000:
        log.warning(f"  Not enough completed candles for {symbol}, skipping.")
        return

    # Generate signal (full: tech + fundamentals + news)
    signal = generate_signal(symbol, completed_candles,
                             include_fundamentals=True,
                             include_news=True,
                             state=state)

    if signal is None:
        reason = _last_rejection_reason.get(symbol, "No signal")
        log.info(f"  {symbol} | No signal ({reason})")
        _cycle_results.append({
            'coin': symbol.replace('USDT', ''), 'price': completed_candles[-1][3],
            'dir': '-', 'tech': 0, 'fund': 0, 'news': 0, 'total': 0,
            'filter_reason': reason, 'tp': 0, 'sl': 0, 'rr_ratio': 0
        })
        return

    direction = signal["direction"]
    total     = signal["score"]
    tech_pts  = signal["technical_score"]
    fund_pts  = signal["fundamental_score"]
    news_pts  = signal["news_score"]

    dbg.debug(f"[{symbol}] Total={total:.2f} threshold={ALERT_THRESHOLD_OPTB} cooldown={not can_alert(state, symbol, direction)}")
    log.info(f"  {symbol} | LONG={signal['long_score']:.1f} SHORT={signal['short_score']:.1f} "
             f"| Winner={direction} TOTAL={total:.1f} | {signal.get('regime','?')}/{signal.get('strategy','?')}")

    # Store for summary table (with position suggestions)
    _cycle_results.append({
        'coin': symbol.replace('USDT', ''),
        'price': signal['entry_price'],
        'dir': direction,
        'tech': tech_pts,
        'fund': fund_pts,
        'news': news_pts,
        'total': total,
        'tp': signal['tp'],
        'sl': signal['sl'],
        'tp_pct': signal['tp_pct'],
        'sl_pct': signal['sl_pct'],
        'atr': signal['atr'],
        'rr_ratio': signal['rr_ratio'],
        'filter_reason': None  # None = signal passed all filters
    })

    # Alert dispatch — all tiers use the same signal (same TP/SL)
    if total >= ALERT_THRESHOLD_HARD and can_alert(state, symbol, direction, tier="HIGH"):
        msg = format_alert(signal)
        send_telegram(msg)
        mark_alert(state, symbol, direction, tier="HIGH")
        save_state(state)
        log.info(f"  🚀 HIGH CONF sent for {symbol} {direction} ({total:.1f})")

    elif total >= ALERT_THRESHOLD_OPTB and can_alert(state, symbol, direction, tier="ENTRY"):
        msg = format_alert(signal)
        send_telegram(msg)
        mark_alert(state, symbol, direction, tier="ENTRY")
        save_state(state)
        log.info(f"  ✅ ENTRY sent for {symbol} {direction} ({total:.1f})")

    elif total >= ALERT_THRESHOLD_SOFT and can_alert(state, symbol, direction, tier="WATCH"):
        coin = symbol.replace("USDT", "")
        rsi_val = signal['details'].get('rsi', {}).get('1h', 'N/A')
        soft_msg = (
            f"🟡 <b>WATCH [{total:.1f}/20]</b>\n"
            f"{'🟢 LONG' if direction == 'LONG' else '🔴 SHORT'} <b>{coin}</b> "
            f"@ <code>${signal['entry_price']:,.4f}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ Building signal — RSI: {rsi_val}\n"
            f"Tech: {tech_pts:.1f} | Fund: {fund_pts:.1f} | News: {news_pts:.1f}\n\n"
            f"📐 <b>If entry:</b> SL {-signal['sl_pct']:+.2f}% | TP {signal['tp_pct']:+.2f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC"
        )
        send_telegram(soft_msg)
        mark_alert(state, symbol, direction, tier="WATCH")
        save_state(state)
        log.info(f"  👀 WATCH sent for {symbol} {direction} ({total:.1f})")
    else:
        log.info(f"  ⏭  Below threshold or in cooldown.")


def main():
    log.info("=" * 50)
    log.info(f"Market Scanner Multi-Regime Starting… (PID: {os.getpid()})")
    log.info(f"Strategies: Trend Pullback | Breakout")
    log.info(f"Symbols: {', '.join(SYMBOLS)}")
    log.info(f"Scan interval: {SCAN_INTERVAL}s")
    log.info(f"Watch: {ALERT_THRESHOLD_SOFT}+ | Entry: {ALERT_THRESHOLD_OPTB}+ | High Conf: {ALERT_THRESHOLD_HARD}+")
    log.info("=" * 50)

    state = load_state()

    send_telegram(
        f"🤖 <b>Market Scanner Multi-Regime Online</b> (PID: {os.getpid()})\n"
        f"📋 Scanning: {', '.join(s.replace('USDT','') for s in SYMBOLS)}\n"
        f"🔀 Strategies: Trend Pullback | Breakout\n"
        f"⏱ Interval: every 5 minutes\n"
        f"👀 Watch: {ALERT_THRESHOLD_SOFT}+ | ✅ Entry: {ALERT_THRESHOLD_OPTB}+ | 🚀 High: {ALERT_THRESHOLD_HARD}+"
    )

    while True:
        cycle_start = time.time()
        log.info(f"\n{'─'*40}\nScan cycle @ {datetime.now(timezone.utc).strftime('%H:%M:%S')}")

        _cycle_results.clear()
        _last_rejection_reason.clear()

        for symbol in SYMBOLS:
            try:
                scan_symbol(symbol, state)
            except Exception as e:
                log.error(f"Error scanning {symbol}: {e}", exc_info=True)
            time.sleep(1.5)

        elapsed = time.time() - cycle_start
        sleep_for = max(0, SCAN_INTERVAL - elapsed)

        # Summary table - show ALL computed signals + rejected ones
        log.info(f"\n{'─'*120}")
        log.info(f"{'Coin':<6} {'Price':>10}  {'Dir':<5} {'Tech':>5} {'Fund':>5} {'News':>5} {'Total':>6} {'Entry':>10} {'TP':>10} {'SL':>10} {'R:R':>4} {'Status':<20}")
        log.info(f"{'─'*120}")

        if _cycle_results:
            for r in sorted(_cycle_results, key=lambda x: x['total'], reverse=True):
                # Format TP/SL with appropriate decimals (more for small-value coins)
                if r['total'] > 0:
                    if r['price'] < 1.0:  # Small coins (DOGE, etc.)
                        tp_str = f"${r['tp']:.6f}"
                        sl_str = f"${r['sl']:.6f}"
                    else:
                        tp_str = f"${r['tp']:,.2f}"
                        sl_str = f"${r['sl']:,.2f}"
                    status = "✅ ACTIVE" if r.get('filter_reason') is None else f"⚠️  {r.get('filter_reason', 'SKIPPED')}"
                else:
                    tp_str = "—"
                    sl_str = "—"
                    status = f"⚠️  {r.get('filter_reason', 'NO SIGNAL')}"
                rr_str = f"{r['rr_ratio']:.1f}:1" if r['total'] > 0 else "—"
                log.info(f"{r['coin']:<6} ${r['price']:>9,.3f}  {r['dir']:<5} {r['tech']:>5.1f} {r['fund']:>5.1f} {r['news']:>5.1f} {r['total']:>6.1f}  {r['price']:>10,.2f}  {tp_str:>12}  {sl_str:>12}  {rr_str:>4} {status:<20}")
        else:
            log.info("(No signals computed this cycle)")
        log.info(f"{'─'*120}")

        log.info(f"Cycle done in {elapsed:.0f}s. Next scan in {sleep_for:.0f}s.")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
