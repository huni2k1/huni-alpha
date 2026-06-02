"""Shared trade execution semantics used by both backtest and live trader.

Modules here are pure functions over candle/signal data — no I/O, no exchange calls.
Imported by backtester, trader, and the analyzer's forward-trade simulator so that
"how a fill is resolved" / "how a position is sized" / "how a signal is gated" is
defined exactly once.
"""
