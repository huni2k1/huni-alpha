import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from trading_bot import statistical_utils as stats_utils


def test_wilson_score_interval_contains_observed_rate():
    low, high = stats_utils.wilson_score_interval(48, 100)
    assert 0 <= low <= 0.48 <= high <= 1


def test_two_proportion_ztest_detects_clear_difference():
    p_value = stats_utils.two_proportion_ztest(60, 100, 35, 100)
    assert p_value < 0.01


def test_benjamini_hochberg_adjusted_reduces_false_discoveries():
    # 5 tests, only first two should survive at q=0.10
    p_values = [0.001, 0.03, 0.06, 0.15, 0.40]
    adj = stats_utils.benjamini_hochberg_adjusted(p_values)
    assert len(adj) == 5
    assert adj[0] <= 0.10  # survives
    assert adj[1] <= 0.10  # survives
    assert adj[2] <= 0.10  # survives (adj = 0.10 exactly)
    assert adj[3] > 0.10   # rejected
    assert adj[4] > 0.10   # rejected


def test_benjamini_hochberg_adjusted_is_monotone():
    p_values = [0.001, 0.03, 0.06, 0.15, 0.40]
    adj = stats_utils.benjamini_hochberg_adjusted(p_values)
    for i in range(len(adj) - 1):
        assert adj[i] <= adj[i + 1], f"Not monotone at index {i}: {adj}"


def test_benjamini_hochberg_adjusted_handles_empty():
    assert stats_utils.benjamini_hochberg_adjusted([]) == []


def test_build_walk_forward_windows_uses_rolling_calendar_months():
    months = [
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        datetime(2025, 2, 1, tzinfo=timezone.utc),
        datetime(2025, 3, 1, tzinfo=timezone.utc),
        datetime(2025, 4, 1, tzinfo=timezone.utc),
        datetime(2025, 5, 1, tzinfo=timezone.utc),
        datetime(2025, 6, 1, tzinfo=timezone.utc),
        datetime(2025, 7, 1, tzinfo=timezone.utc),
        datetime(2025, 8, 1, tzinfo=timezone.utc),
        datetime(2025, 9, 1, tzinfo=timezone.utc),
        datetime(2025, 10, 1, tzinfo=timezone.utc),
        datetime(2025, 11, 1, tzinfo=timezone.utc),
        datetime(2025, 12, 1, tzinfo=timezone.utc),
    ]
    windows = stats_utils.build_walk_forward_windows(months, train_months=6, test_months=3, step_months=3)
    assert windows == [
        {
            "train_months": ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"],
            "test_months": ["2025-07", "2025-08", "2025-09"],
        },
        {
            "train_months": ["2025-04", "2025-05", "2025-06", "2025-07", "2025-08", "2025-09"],
            "test_months": ["2025-10", "2025-11", "2025-12"],
        },
    ]
