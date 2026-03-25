#!/usr/bin/env python3
"""
Quick check: What does generate_signal() say right now?
Run from the same directory as market-scanner.py:
  python3 check-signal.py
  python3 check-signal.py ETHUSDT SOLUSDT   # check multiple symbols
  python3 check-signal.py all               # scan all 10 symbols
"""
import sys, os

# Suppress scanner side effects
_orig_makedirs = os.makedirs
os.makedirs = lambda *a, **kw: None
import logging
_orig_FH = logging.FileHandler
logging.FileHandler = lambda *a, **kw: logging.NullHandler()

import importlib.util
scanner_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner", "market-scanner.py")
spec = importlib.util.spec_from_file_location("scanner", scanner_path)
scanner = importlib.util.module_from_spec(spec)
scanner.os = os
scanner.logging = logging
scanner.sys = sys
spec.loader.exec_module(scanner)

os.makedirs = _orig_makedirs
logging.FileHandler = _orig_FH

ALL_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
]

args = sys.argv[1:]
if not args:
    symbols = ["BTCUSDT"]
elif args[0].lower() == "all":
    symbols = ALL_SYMBOLS
else:
    symbols = [s.upper() for s in args]

for symbol in symbols:
    print(f"\n{'='*60}")
    print(f"  {symbol} — generate_signal() (tech-only, 4h)")
    print(f"{'='*60}")

    candles = scanner.fetch_klines(symbol, "4h", 1000)
    if not candles:
        print("  ERROR: Could not fetch candles")
        continue

    print(f"  Candles: {len(candles)} | Latest close: ${candles[-1][3]:,.2f}")

    # Always show raw technical breakdown
    tech = scanner.score_technical(symbol, candles)
    d = tech["details"]

    print(f"\n  --- Regime & Strategy ---")
    print(f"  Regime:       {d.get('regime', 'N/A')}")
    print(f"  Strategy:     {d.get('strategy', 'N/A')}")
    print(f"  BB Bandwidth: {d.get('bb_bandwidth', 'N/A')}")
    print(f"  BB Squeeze:   {d.get('bb_squeeze', 'N/A')}")

    print(f"\n  --- Indicators ---")
    print(f"  RSI 4h:       {d.get('rsi', {}).get('4h', 'N/A')}")
    print(f"  ADX:          {d.get('adx', 'N/A')}")
    print(f"  MACD line:    {d.get('macd_line', 'N/A')}")
    print(f"  Signal line:  {d.get('signal_line', 'N/A')}")
    print(f"  Histogram:    {d.get('hist_curr', 'N/A')} (prev: {d.get('hist_prev', 'N/A')})")
    print(f"  Vol ratio:    {d.get('vol_ratio', 'N/A')}")
    print(f"  EMA200:       ${d.get('ema200', 0):,.2f}")
    print(f"  Above EMA200: {d.get('above_e200', 'N/A')}")
    print(f"  EMA bull:     {d.get('ema_bull', 'N/A')} (9>21>50)")
    print(f"  EMA bear:     {d.get('ema_bear', 'N/A')} (9<21<50)")

    print(f"\n  --- Raw Scores ---")
    print(f"  Long score:   {tech['long_score']}")
    print(f"  Short score:  {tech['short_score']}")

    signal = scanner.generate_signal(symbol, candles,
                                      include_fundamentals=False,
                                      include_news=False)

    if signal is None:
        print(f"\n  >>> RESULT: NO SIGNAL (neutral or score=0)")
        if d.get('adx', 0) < 20 and d.get('regime') == 'ranging':
            print(f"      Regime: ranging — mean reversion active but score too low")
        elif d.get('adx', 0) < 25:
            print(f"      ADX={d.get('adx',0)} (weak trend) — signal penalized")
        if not d.get('ema_bull') and not d.get('ema_bear'):
            print(f"      EMAs tangled — no clear alignment")
    else:
        tier = "HIGH CONFIDENCE" if signal["score"] >= 8 else \
               "ENTRY" if signal["score"] >= 6 else \
               "WATCH" if signal["score"] >= 5 else "BELOW THRESHOLD"

        print(f"\n  >>> SIGNAL: {signal['direction']} | Score: {signal['score']}/20 | Tier: {tier}")
        print(f"      Regime:   {signal.get('regime', 'N/A')}")
        print(f"      Strategy: {signal.get('strategy', 'N/A')}")
        print(f"      Entry:    ${signal['entry_price']:,.4f}")
        print(f"      TP:       ${signal['tp']:,.4f}  (+{signal['tp_pct']:.2f}%)")
        print(f"      SL:       ${signal['sl']:,.4f}  (-{signal['sl_pct']:.2f}%)")
        print(f"      ATR:      ${signal['atr']:,.4f}")
        print(f"      R:R:      {signal['rr_ratio']}:1")

print()
