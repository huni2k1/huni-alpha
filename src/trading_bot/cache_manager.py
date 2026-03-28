#!/usr/bin/env python3
"""
Cache Manager — Manage local candle cache

Usage:
  python3 -m trading_bot.cache_manager stats         # Show cache stats
  python3 -m trading_bot.cache_manager clear         # Clear all cache
  python3 -m trading_bot.cache_manager clear BTCUSDT # Clear BTC cache
"""

import sys
import argparse
from . import candle_cache


def cmd_stats(args):
    """Show cache statistics."""
    stats = candle_cache.get_cache_stats()
    print("\n" + "="*60)
    print("CANDLE CACHE STATISTICS")
    print("="*60)
    print(f"Total cached files:  {stats['total_files']}")
    print(f"Total cache size:    {stats['total_size_mb']:.2f} MB")
    print(f"Cached symbols:      {', '.join(stats['symbols']) if stats['symbols'] else 'None'}")
    print("="*60 + "\n")


def cmd_clear(args):
    """Clear cache."""
    symbol = args.symbol if hasattr(args, 'symbol') and args.symbol else None

    if symbol:
        print(f"Clearing cache for {symbol}...")
    else:
        print("Clearing ALL cache...")
        response = input("Are you sure? (yes/no): ")
        if response.lower() != "yes":
            print("Cancelled.")
            return

    if candle_cache.clear_cache(symbol):
        print("✓ Cache cleared successfully")
    else:
        print("✗ Error clearing cache")


def main():
    parser = argparse.ArgumentParser(
        description="Manage candle cache",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 cache_manager.py stats              # Show cache stats
  python3 cache_manager.py clear              # Clear all cache
  python3 cache_manager.py clear BTCUSDT      # Clear BTC cache
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Stats command
    subparsers.add_parser("stats", help="Show cache statistics")

    # Clear command
    clear_parser = subparsers.add_parser("clear", help="Clear cache")
    clear_parser.add_argument(
        "symbol", nargs="?", default=None,
        help="Symbol to clear (omit to clear all)"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "stats":
        cmd_stats(args)
    elif args.command == "clear":
        cmd_clear(args)


if __name__ == "__main__":
    main()
