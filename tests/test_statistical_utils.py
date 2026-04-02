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
