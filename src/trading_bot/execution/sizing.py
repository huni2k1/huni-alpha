"""Risk-based position sizing — shared by live trader and backtester.

This is the one formula that decides how much money a position occupies.
Both callers (`trader.size_position` and `backtester.run_backtest`) must
agree on it byte-for-byte; the function lives here so future drift is
structurally impossible.

Callers wrap this with their own context-specific concerns:
  - Trader adds: quantity conversion, exchange filters (min_qty, min_notional,
    qty_precision).
  - Backtester adds: Kelly multiplier (technical engine), fixed-size override,
    reset-monthly equity choice.
"""

from __future__ import annotations


def size_position_notional(
    equity: float,
    risk_pct: float,
    sl_pct: float,
    *,
    max_positions: int = 3,
) -> float:
    """Return the USDT notional to deploy on this trade.

    Formula:
        risk_amount    = equity × risk_pct%
        position_usdt  = risk_amount / sl_pct%
        cap            = equity / max_positions
        return         = min(position_usdt, cap)

    `equity` must be total wallet equity (free + locked + unrealized PnL),
    not the available USDT balance — otherwise successive positions shrink
    as margin gets locked.

    Returns 0.0 on invalid inputs (sl_pct ≤ 0 or equity ≤ 0 or max_positions ≤ 0).
    """
    if sl_pct <= 0 or equity <= 0 or max_positions <= 0:
        return 0.0

    risk_amount = equity * (risk_pct / 100.0)
    position_usdt = risk_amount / (sl_pct / 100.0)
    cap = equity / max_positions
    return min(position_usdt, cap)
