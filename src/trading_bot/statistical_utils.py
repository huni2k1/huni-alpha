"""
Statistical helper utilities for the setup analyzer.

These utilities are intentionally lightweight so the analyzer can run
without adding new third-party dependencies beyond the existing stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import erf, sqrt
from typing import Iterable, Sequence


MIN_SAMPLE_P_VALUE = 1.0


def normal_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def wilson_score_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """
    Wilson score interval for a binomial proportion.

    Returns low/high bounds in the 0-1 range.
    """
    if total <= 0:
        return 0.0, 0.0

    p_hat = successes / total
    denominator = 1.0 + (z ** 2) / total
    center = (p_hat + (z ** 2) / (2.0 * total)) / denominator
    margin = (
        z
        * sqrt((p_hat * (1.0 - p_hat) / total) + (z ** 2) / (4.0 * total ** 2))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def two_proportion_ztest(
    successes_a: int,
    total_a: int,
    successes_b: int,
    total_b: int,
) -> float:
    """
    Two-sided z-test for difference in proportions.

    Returns a p-value in the 0-1 range.
    """
    if total_a <= 0 or total_b <= 0:
        return MIN_SAMPLE_P_VALUE

    p1 = successes_a / total_a
    p2 = successes_b / total_b
    pooled = (successes_a + successes_b) / (total_a + total_b)
    variance = pooled * (1.0 - pooled) * ((1.0 / total_a) + (1.0 / total_b))
    if variance <= 0:
        return MIN_SAMPLE_P_VALUE

    z_score = (p1 - p2) / sqrt(variance)
    p_value = 2.0 * (1.0 - normal_cdf(abs(z_score)))
    return max(0.0, min(1.0, p_value))


def profit_factor(pnl_values: Iterable[float]) -> float:
    """Calculate profit factor from a sequence of PnL percentages."""
    gross_profit = 0.0
    gross_loss = 0.0
    for value in pnl_values:
        if value > 0:
            gross_profit += value
        elif value < 0:
            gross_loss += abs(value)

    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


@dataclass
class SampleStats:
    """Summary metrics for a collection of outcomes."""

    trades: int
    wins: int
    losses: int
    win_rate: float
    avg_pnl_pct: float
    profit_factor: float
    pnl_sum_pct: float
    wilson_low: float
    wilson_high: float

    def to_dict(self) -> dict:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 2),
            "avg_pnl_pct": round(self.avg_pnl_pct, 4),
            "profit_factor": round(self.profit_factor, 4)
            if self.profit_factor != float("inf")
            else float("inf"),
            "pnl_sum_pct": round(self.pnl_sum_pct, 4),
            "wilson_low": round(self.wilson_low * 100.0, 2),
            "wilson_high": round(self.wilson_high * 100.0, 2),
        }


def summarize_outcomes(outcomes: Sequence[dict]) -> SampleStats:
    """Summarize a list of outcome dicts with `is_win` and `pnl_pct` keys."""
    trades = len(outcomes)
    if trades == 0:
        return SampleStats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    wins = sum(1 for outcome in outcomes if outcome.get("is_win"))
    losses = trades - wins
    pnl_values = [float(outcome.get("pnl_pct", 0.0)) for outcome in outcomes]
    avg_pnl_pct = sum(pnl_values) / trades
    pnl_sum_pct = sum(pnl_values)
    pf = profit_factor(pnl_values)
    wilson_low, wilson_high = wilson_score_interval(wins, trades)
    return SampleStats(
        trades=trades,
        wins=wins,
        losses=losses,
        win_rate=(wins / trades) * 100.0,
        avg_pnl_pct=avg_pnl_pct,
        profit_factor=pf,
        pnl_sum_pct=pnl_sum_pct,
        wilson_low=wilson_low,
        wilson_high=wilson_high,
    )


def month_floor(dt: datetime) -> datetime:
    """Return the month-start timestamp in UTC for the supplied datetime."""
    dt = dt.astimezone(timezone.utc)
    return datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)


def add_months(dt: datetime, months: int) -> datetime:
    """Add whole calendar months to a UTC datetime."""
    dt = dt.astimezone(timezone.utc)
    month_index = (dt.year * 12 + (dt.month - 1)) + months
    year = month_index // 12
    month = (month_index % 12) + 1
    return datetime(year, month, 1, tzinfo=timezone.utc)


def build_walk_forward_windows(
    month_starts: Sequence[datetime],
    train_months: int = 6,
    test_months: int = 3,
    step_months: int = 3,
) -> list[dict]:
    """
    Build rolling walk-forward train/test windows from sorted month starts.

    Example:
      train 6 / test 3 / step 3 over 12 months yields:
      months 1-6 train, 7-9 test
      months 4-9 train, 10-12 test
    """
    if train_months <= 0 or test_months <= 0 or step_months <= 0:
        raise ValueError("train_months, test_months, and step_months must be positive")

    unique_months = sorted({month_floor(month) for month in month_starts})
    windows = []
    span = train_months + test_months
    for start_idx in range(0, max(0, len(unique_months) - span + 1), step_months):
        train_slice = unique_months[start_idx:start_idx + train_months]
        test_slice = unique_months[start_idx + train_months:start_idx + span]
        if len(train_slice) < train_months or len(test_slice) < test_months:
            continue
        windows.append(
            {
                "train_months": [m.strftime("%Y-%m") for m in train_slice],
                "test_months": [m.strftime("%Y-%m") for m in test_slice],
            }
        )
    return windows
