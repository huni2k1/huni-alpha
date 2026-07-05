"""Live-vs-backtest parity watchdog.

The June 2026 forming-candle bug traded unvalidated signals for three weeks
before a manual audit caught it: 67 of 88 live trades didn't exist in the
backtest over the same window. This module automates that audit.

Run weekly on the droplet (systemd timer, see deploy/huni-parity.*):

    PYTHONPATH=src python -m trading_bot.parity_check

It compares the live trade_log against a fresh backtest over the same window
and Telegram-alerts when they diverge. It also checks behavioral invariants
that catch whole bug classes without needing a backtest at all:

  - entry-minute distribution: with drop_forming in place, entries happen in
    the first scan after an hourly close (minute 0-14). Later entries mean
    the forming-candle bug (or a cousin) is back.
  - trade frequency: the validated strategy averages ~3-4 entries/day across
    the book; a multiple of that means runaway signal generation.

Alerts are advisory — nothing here touches trading state.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

# Thresholds — deliberately loose; this is a tripwire, not a metric dashboard.
FORMING_SHARE_ALERT = 0.20      # >20% of entries at minute >=15 of the hour
COUNT_RATIO_ALERT = 1.5         # live trades > 1.5x backtest trades (or < 1/1.5)
MATCH_RATE_ALERT = 0.50         # <50% of live trades found in backtest
PNL_GAP_ALERT_USD = -25.0       # live pnl trails backtest pnl by > $25
MATCH_WINDOW_HOURS = 2.0        # entry-time tolerance when pairing trades


def _parse_ts(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def entry_minute_violation_share(trades: list[dict]) -> float:
    """Share of trades entered at minute >= 15 of the hour.

    Closed-candle signals are acted on in the first scans after the hourly
    close (scan interval 300s → minute 0-14 is the legitimate window; a slot
    freed mid-hour may add a few stragglers, hence the alert threshold).
    """
    minutes = [ts.minute for t in trades if (ts := _parse_ts(t.get("entry_time")))]
    if not minutes:
        return 0.0
    return sum(1 for m in minutes if m >= 15) / len(minutes)


def match_trades(live: list[dict], bt: list[dict],
                 window_hours: float = MATCH_WINDOW_HOURS):
    """Pair live and backtest trades by symbol + direction + entry proximity.

    Returns (matched_pairs, live_only, bt_only).
    """
    used: set[int] = set()
    matched: list[tuple[dict, dict]] = []
    for lt in live:
        le = _parse_ts(lt.get("entry_time"))
        if le is None:
            continue
        best, best_delta = None, None
        for i, bt_trade in enumerate(bt):
            if i in used:
                continue
            if bt_trade.get("symbol") != lt.get("symbol"):
                continue
            if bt_trade.get("direction") != lt.get("direction"):
                continue
            be = _parse_ts(bt_trade.get("entry_time"))
            if be is None:
                continue
            delta = abs((be - le).total_seconds())
            if delta <= window_hours * 3600 and (best_delta is None or delta < best_delta):
                best, best_delta = i, delta
        if best is not None:
            used.add(best)
            matched.append((lt, bt[best]))
    matched_live = {id(l) for l, _ in matched}
    live_only = [t for t in live if id(t) not in matched_live]
    bt_only = [t for i, t in enumerate(bt) if i not in used]
    return matched, live_only, bt_only


def _pnl(trades: list[dict]) -> float:
    return sum(float(t.get("pnl_usd", 0) or 0) for t in trades)


def build_report(live: list[dict], bt: list[dict], days: int) -> tuple[str, list[str]]:
    """Build the Telegram report and the list of triggered alerts."""
    alerts: list[str] = []

    forming_share = entry_minute_violation_share(live)
    if forming_share > FORMING_SHARE_ALERT:
        alerts.append(
            f"{forming_share:.0%} of live entries at minute ≥15 — "
            f"forming-candle regression? (limit {FORMING_SHARE_ALERT:.0%})"
        )

    matched, live_only, bt_only = match_trades(live, bt)
    match_rate = (len(matched) / len(live)) if live else 1.0
    if live and match_rate < MATCH_RATE_ALERT:
        alerts.append(f"match rate {match_rate:.0%} — live is trading a different book than the backtest")

    if bt:
        ratio = len(live) / len(bt)
        if ratio > COUNT_RATIO_ALERT or ratio < 1 / COUNT_RATIO_ALERT:
            alerts.append(f"trade count live/backtest = {len(live)}/{len(bt)} ({ratio:.2f}x)")

    live_pnl, bt_pnl = _pnl(live), _pnl(bt)
    if live_pnl - bt_pnl < PNL_GAP_ALERT_USD:
        alerts.append(f"live pnl trails backtest by ${bt_pnl - live_pnl:.2f}")

    status = "🚨 <b>PARITY ALERT</b>" if alerts else "✅ <b>Parity OK</b>"
    lines = [
        f"{status} — last {days}d",
        f"Live: {len(live)} trades, ${live_pnl:+.2f} | Backtest: {len(bt)} trades, ${bt_pnl:+.2f}",
        f"Matched: {len(matched)} ({match_rate:.0%} of live) | live-only: {len(live_only)} | bt-only: {len(bt_only)}",
        f"Entries at minute ≥15: {forming_share:.0%}",
    ]
    lines += [f"⚠️ {a}" for a in alerts]
    return "\n".join(lines), alerts


def main() -> int:
    from .binance_http import send_telegram
    from .logging_setup import configure_logging, log
    configure_logging()

    days = int(os.environ.get("PARITY_DAYS", "14"))
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    state_file = os.environ.get(
        "TRADER_STATE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "trader-state.json"),
    )
    try:
        with open(state_file) as f:
            state = json.load(f)
    except Exception as exc:
        log.error(f"parity_check: cannot read state file {state_file}: {exc}")
        return 1

    live = [
        t for t in state.get("trade_log", [])
        if (ts := _parse_ts(t.get("exit_time"))) and ts >= start
    ]

    # Fresh backtest over the same window, prod-equivalent config.
    from .backtester import run_backtest
    from .signals.config import SYMBOLS
    results = run_backtest(
        symbols=list(SYMBOLS),
        months=1,  # ignored when start/end given
        account=1000.0,
        signal_engine="combined",
        start_date=start,
        end_date=now,
    )
    bt = results.get("trades", [])

    report, alerts = build_report(live, bt, days)
    plain = report.replace("<b>", "").replace("</b>", "")
    log.info("parity_check:\n" + plain)
    print(plain, flush=True)  # journalctl visibility under systemd (no tty)
    send_telegram(report)
    # Exit 2 signals "alerts found" — the systemd unit whitelists it via
    # SuccessExitStatus so a firing watchdog doesn't show as a failed unit.
    return 2 if alerts else 0


if __name__ == "__main__":
    raise SystemExit(main())
