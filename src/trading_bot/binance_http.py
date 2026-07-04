"""HTTP helpers: Binance kline fetching (with on-disk cache) and Telegram alerts.

This file contains zero signal logic. Other modules import these helpers when
they need to talk to Binance or push a Telegram message.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from . import candle_cache
from .logging_setup import log


# ── Telegram credentials ─────────────────────────────────────────────────────
_tg_token = os.environ.get("TELEGRAM_TOKEN", "")
_tg_chat = os.environ.get("TELEGRAM_CHAT", "")

# Fall back to config/telegram.json if env vars not set
if not _tg_token or not _tg_chat:
    try:
        _config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "telegram.json")
        with open(_config_path) as f:
            _tg_config = json.load(f)
            _tg_token = _tg_token or _tg_config.get("token", "")
            _tg_chat = _tg_chat or _tg_config.get("chat_id", "")
    except Exception:
        pass

TELEGRAM_TOKEN = _tg_token
TELEGRAM_CHAT = _tg_chat


# ── HTTP session ─────────────────────────────────────────────────────────────
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
                time.sleep(0.5)
            else:
                log.debug(f"GET {url} failed after {max_retries} attempts: {e}")
    return None


# ── Binance kline fetching ───────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str, limit: int) -> Optional[list]:
    """Fetch klines with pagination support (Binance API limit is 1000)."""
    if limit <= 1000:
        data = get("https://api.binance.com/api/v3/klines",
                   {"symbol": symbol, "interval": interval, "limit": limit})
        if not data:
            return None
        if len(data) < int(limit * 0.95):
            log.warning(f"  {symbol}: Incomplete data — requested {limit} candles, got {len(data)}")
            return None
        return [[float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]),
                 float(c[7]), float(c[10])]
                for c in data]

    all_candles = []
    all_raw = []
    remaining = limit
    end_time = None

    while remaining > 0:
        batch_size = min(1000, remaining)
        params = {"symbol": symbol, "interval": interval, "limit": batch_size}
        if end_time is not None:
            params["endTime"] = end_time

        data = get("https://api.binance.com/api/v3/klines", params)
        if not data or len(data) == 0:
            break

        batch = [[float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]),
                  float(c[7]), float(c[10])]
                 for c in data]
        batch_raw = data

        all_candles = batch + all_candles
        all_raw = batch_raw + all_raw
        remaining -= len(batch)

        if data:
            oldest_open_time_ms = int(data[0][0])
            end_time = oldest_open_time_ms - 1

        if len(batch) < batch_size:
            break

    if not all_candles:
        return None

    min_expected = int(limit * 0.95)
    if len(all_candles) < min_expected:
        log.warning(f"  {symbol}: Incomplete data — requested {limit} candles, got {len(all_candles)} (min {min_expected})")
        return None

    # Gap detection
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
                        use_cache: bool = True,
                        drop_forming: bool = False) -> Optional[list]:
    """Fetch klines with local disk caching for speed.

    For backtesting: caches entire date ranges to avoid repeated API calls.
    For live scanning: uses cache if available, falls back to API.

    drop_forming: drop the last (still-forming) candle so signals are computed
    on CLOSED candles only — the same data the analyzer mined and the
    backtester replays. Binance always includes the current forming candle as
    the final element of a klines response. Live callers must pass True;
    leaving it in creates intra-candle phantom signals that were never
    validated (confirmed live: 67 of 88 trades in Jun 13–Jul 4 were
    forming-candle entries invisible to the backtest, net −$58).
    """
    if not use_cache:
        candles = fetch_klines(symbol, interval, limit)
        if drop_forming and candles:
            return candles[:-1]
        return candles

    now = datetime.now(timezone.utc)
    candles_per_day = 24 if interval == "1h" else 6
    days_needed = (limit + candles_per_day - 1) // candles_per_day
    start_date = (now - timedelta(days=days_needed)).date()
    end_date = now.date()

    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    cached_candles = candle_cache.load_from_cache(symbol, interval, start_str, end_str)
    if cached_candles is not None:
        log.debug(f"  {symbol}: Loaded {len(cached_candles)} candles from cache")
        result = cached_candles[-limit:] if len(cached_candles) > limit else cached_candles
        # Cache ranges ending today may include a candle that was still forming
        # when cached — drop defensively (one closed candle is cheap vs 1000).
        if drop_forming and result:
            return result[:-1]
        return result

    log.debug(f"  {symbol}: Cache miss, fetching from Binance API...")
    candles = fetch_klines(symbol, interval, limit)

    if candles is not None:
        candle_cache.save_to_cache(symbol, interval, start_str, end_str, candles)
        log.debug(f"  {symbol}: Cached {len(candles)} candles for {start_str}–{end_str}")

    if drop_forming and candles:
        return candles[:-1]
    return candles


# ── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram credentials not set, skipping send.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.debug("Telegram message sent.")
    except Exception as e:
        log.error(f"Telegram error: {e}")
