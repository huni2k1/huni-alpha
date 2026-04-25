"""
Candle Data Caching System

Stores fetched OHLCV candles locally to avoid repeated API calls.
Implements intelligent cache invalidation and update strategies.

Cache Structure:
  ~/.trading_bot_cache/
  ├── BTCUSDT/
  │   ├── 1h/
  │   │   └── 2024-01-01_2026-03-28.json    (range cache)
  │   └── 4h/
  │       └── 2024-01-01_2026-03-28.json
  ├── ETHUSDT/
  └── ...
"""

import os
import json
import time
from datetime import datetime
from typing import Optional, List

CACHE_DIR = os.path.expanduser("~/.trading_bot_cache")
DEFAULT_CACHE_VARIANT = "ohlcv"
SCHEMA_VERSION = 2


def _ensure_cache_dir():
    """Create cache directory structure if needed."""
    os.makedirs(CACHE_DIR, exist_ok=True)


def _get_cache_path(symbol: str, interval: str, start_date: str, end_date: str,
                   variant: str = DEFAULT_CACHE_VARIANT) -> str:
    """Get cache file path for symbol/interval/date range.

    Args:
        symbol: Trading pair (e.g., "BTCUSDT")
        interval: Candle interval (e.g., "1h", "4h")
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)

    Returns:
        Full path to cache file
    """
    _ensure_cache_dir()
    symbol_dir = os.path.join(CACHE_DIR, symbol, interval)
    os.makedirs(symbol_dir, exist_ok=True)
    if variant == DEFAULT_CACHE_VARIANT:
        filename = f"{start_date}_{end_date}.json"
    else:
        safe_variant = variant.replace(os.sep, "_")
        filename = f"{start_date}_{end_date}.{safe_variant}.json"
    return os.path.join(symbol_dir, filename)


def _date_range_to_days(start_date: str, end_date: str) -> int:
    """Calculate number of days between two dates.

    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)

    Returns:
        Number of days
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    return (end - start).days


def _candle_count_from_days(days: int, interval: str) -> int:
    """Estimate expected candle count for date range.

    Args:
        days: Number of days in range
        interval: Candle interval ("1h", "4h")

    Returns:
        Expected candle count (approximate)
    """
    candles_per_day = {"1h": 24, "4h": 6}
    return days * candles_per_day.get(interval, 24)


def load_from_cache(symbol: str, interval: str, start_date: str,
                   end_date: str, min_completeness: float = 0.95,
                   variant: str = DEFAULT_CACHE_VARIANT) -> Optional[List]:
    """Load candles from cache if available and valid.

    Args:
        symbol: Trading pair
        interval: Candle interval
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        min_completeness: Minimum data completeness (0-1.0)

    Returns:
        List of candles [[open, high, low, close, volume], ...] or None
    """
    cache_path = _get_cache_path(symbol, interval, start_date, end_date, variant=variant)

    if not os.path.exists(cache_path):
        return None

    try:
        with open(cache_path, 'r') as f:
            cache_data = json.load(f)

        # Validate cache entry
        candles = cache_data.get("candles", [])
        cached_at = cache_data.get("cached_at", 0)
        cache_age_hours = (time.time() - cached_at) / 3600

        # Cache is valid if:
        # 1. Not older than 24 hours (for recent data, refresh daily)
        # 2. Has minimum expected candles (>95% completeness)
        expected_count = _candle_count_from_days(
            _date_range_to_days(start_date, end_date),
            interval
        )
        completeness = len(candles) / expected_count if expected_count > 0 else 0

        is_recent = cache_age_hours < 24
        is_complete = completeness >= min_completeness

        if is_complete and is_recent:
            return candles

        if is_complete and not is_recent:
            # Cache is complete but stale (>24h old); warn and force a refresh.
            print(f"[Cache] ⚠ {cache_path} is {cache_age_hours:.1f}h old (complete but stale)")

        return None

    except Exception as e:
        print(f"[Cache] Error loading {cache_path}: {e}")
        return None


def save_to_cache(symbol: str, interval: str, start_date: str,
                 end_date: str, candles: List,
                 variant: str = DEFAULT_CACHE_VARIANT) -> bool:
    """Save candles to cache.

    Args:
        symbol: Trading pair
        interval: Candle interval
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        candles: List of candles to cache

    Returns:
        True if saved successfully, False otherwise
    """
    cache_path = _get_cache_path(symbol, interval, start_date, end_date, variant=variant)

    try:
        cache_data = {
            "schema_version": SCHEMA_VERSION,
            "variant": variant,
            "symbol": symbol,
            "interval": interval,
            "start_date": start_date,
            "end_date": end_date,
            "cached_at": time.time(),
            "count": len(candles),
            "candles": candles,
        }

        with open(cache_path, 'w') as f:
            json.dump(cache_data, f, indent=2)

        return True

    except Exception as e:
        print(f"[Cache] Error saving {cache_path}: {e}")
        return False


def get_cache_stats() -> dict:
    """Get statistics about cache usage.

    Returns:
        Dict with cache stats (total files, total size, symbols, etc.)
    """
    if not os.path.exists(CACHE_DIR):
        return {"total_files": 0, "total_size_mb": 0, "symbols": []}

    total_files = 0
    total_size = 0
    symbols = set()

    for root, _, files in os.walk(CACHE_DIR):
        for file in files:
            if file.endswith(".json"):
                total_files += 1
                file_path = os.path.join(root, file)
                total_size += os.path.getsize(file_path)

                # Extract symbol from path
                parts = root.split(os.sep)
                if len(parts) >= 2:
                    symbols.add(parts[-2])

    return {
        "total_files": total_files,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "symbols": sorted(list(symbols)),
    }


def clear_cache(symbol: Optional[str] = None) -> bool:
    """Clear cache files.

    Args:
        symbol: Optional symbol to clear (None = clear all)

    Returns:
        True if successful
    """
    if not os.path.exists(CACHE_DIR):
        return True

    try:
        if symbol:
            # Clear cache for specific symbol
            symbol_dir = os.path.join(CACHE_DIR, symbol)
            if os.path.exists(symbol_dir):
                import shutil
                shutil.rmtree(symbol_dir)
        else:
            # Clear all cache
            import shutil
            shutil.rmtree(CACHE_DIR)
            _ensure_cache_dir()

        return True
    except Exception as e:
        print(f"[Cache] Error clearing cache: {e}")
        return False
