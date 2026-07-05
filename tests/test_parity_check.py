"""Unit tests for the parity watchdog's pure functions."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_bot.parity_check import (
    build_report,
    entry_minute_violation_share,
    match_trades,
)


def _trade(symbol="BTCUSDT", direction="LONG", entry="2026-07-01T14:00:00+00:00", pnl=1.0):
    return {"symbol": symbol, "direction": direction, "entry_time": entry, "pnl_usd": pnl}


# ── entry-minute invariant (forming-candle detector) ─────────────────────────

def test_minute_share_zero_when_entries_right_after_close():
    trades = [_trade(entry=f"2026-07-01T{h:02d}:04:00+00:00") for h in range(10)]
    assert entry_minute_violation_share(trades) == 0.0


def test_minute_share_flags_mid_hour_entries():
    trades = [
        _trade(entry="2026-07-01T14:03:00+00:00"),
        _trade(entry="2026-07-01T15:25:00+00:00"),  # violation
        _trade(entry="2026-07-01T16:45:00+00:00"),  # violation
        _trade(entry="2026-07-01T17:10:00+00:00"),
    ]
    assert entry_minute_violation_share(trades) == 0.5


def test_minute_share_empty_is_zero():
    assert entry_minute_violation_share([]) == 0.0


# ── trade matching ────────────────────────────────────────────────────────────

def test_match_pairs_same_symbol_direction_within_window():
    live = [_trade(entry="2026-07-01T14:05:00+00:00")]
    bt = [_trade(entry="2026-07-01T15:00:00+00:00")]
    matched, live_only, bt_only = match_trades(live, bt)
    assert len(matched) == 1 and not live_only and not bt_only


def test_match_rejects_outside_window_and_wrong_direction():
    live = [
        _trade(entry="2026-07-01T14:00:00+00:00"),
        _trade(direction="SHORT", entry="2026-07-01T14:00:00+00:00"),
    ]
    bt = [
        _trade(entry="2026-07-01T20:00:00+00:00"),   # 6h away — no match
        _trade(entry="2026-07-01T14:30:00+00:00"),   # matches the LONG only
    ]
    matched, live_only, bt_only = match_trades(live, bt)
    assert len(matched) == 1
    assert matched[0][0]["direction"] == "LONG"
    assert len(live_only) == 1 and live_only[0]["direction"] == "SHORT"
    assert len(bt_only) == 1


def test_match_consumes_each_bt_trade_once():
    live = [_trade(entry="2026-07-01T14:00:00+00:00"), _trade(entry="2026-07-01T14:10:00+00:00")]
    bt = [_trade(entry="2026-07-01T14:05:00+00:00")]
    matched, live_only, _ = match_trades(live, bt)
    assert len(matched) == 1 and len(live_only) == 1


# ── report + alerts ───────────────────────────────────────────────────────────

def test_report_ok_when_books_agree():
    live = [_trade(entry=f"2026-07-01T{h:02d}:05:00+00:00") for h in range(8)]
    bt = [_trade(entry=f"2026-07-01T{h:02d}:00:00+00:00") for h in range(8)]
    text, alerts = build_report(live, bt, days=14)
    assert alerts == []
    assert "Parity OK" in text


def test_report_alerts_on_divergent_books():
    # live trades mid-hour, none matching the backtest, heavy losses
    live = [
        _trade(entry=f"2026-07-01T{h:02d}:30:00+00:00", symbol="ADAUSDT", pnl=-5.0)
        for h in range(10)
    ]
    bt = [_trade(entry="2026-07-02T05:00:00+00:00", pnl=3.0)]
    text, alerts = build_report(live, bt, days=14)
    assert "PARITY ALERT" in text
    assert len(alerts) >= 3  # forming-candle share, match rate, count ratio, pnl gap
