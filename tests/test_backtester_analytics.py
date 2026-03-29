import importlib.util
import os
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
backtester_path = os.path.join(os.path.dirname(__file__), "..", "src", "trading_bot", "backtester.py")
spec = importlib.util.spec_from_file_location("backtester", backtester_path)
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)


def make_trade(
    *,
    strategy,
    regime,
    direction,
    exit_reason,
    entry_time,
    pnl_pct,
    pnl_usd,
    score,
    sl_pct,
    duration_hours,
    mfe,
    mae,
):
    return {
        "symbol": "BTCUSDT",
        "direction": direction,
        "strategy": strategy,
        "regime": regime,
        "score": score,
        "entry_time": entry_time,
        "exit_reason": exit_reason,
        "pnl_pct": pnl_pct,
        "pnl_usd": pnl_usd,
        "sl_pct": sl_pct,
        "duration_hours": duration_hours,
        "max_favorable_pct": mfe,
        "max_adverse_pct": mae,
    }


def test_risk_metrics_annualize_partial_year_returns():
    monthly = [{"monthly_return_pct": 5.0} for _ in range(6)]

    metrics = bt._calculate_risk_metrics(
        monthly_sorted=monthly,
        total_return_pct=30.0,
        max_drawdown_pct=10.0,
        months_tested=6,
    )

    assert metrics["months_tested"] == 6
    assert metrics["positive_months"] == 6
    assert metrics["consistency_pct"] == 100.0
    assert metrics["annualized_return_pct"] == pytest.approx(69.0, abs=0.01)
    assert metrics["calmar_ratio"] == pytest.approx(6.9, abs=0.01)
    assert metrics["recovery_factor"] == pytest.approx(3.0, abs=0.01)


def test_trade_breakdowns_include_setup_exit_and_time_buckets():
    trades = [
        make_trade(
            strategy="breakout",
            regime="volatile",
            direction="SHORT",
            exit_reason="TP",
            entry_time="2026-03-24T13:00:00+00:00",
            pnl_pct=2.5,
            pnl_usd=25.0,
            score=6.8,
            sl_pct=1.0,
            duration_hours=4,
            mfe=3.2,
            mae=0.7,
        ),
        make_trade(
            strategy="breakout",
            regime="volatile",
            direction="LONG",
            exit_reason="SL",
            entry_time="2026-03-24T13:00:00+00:00",
            pnl_pct=-1.2,
            pnl_usd=-12.0,
            score=6.4,
            sl_pct=1.5,
            duration_hours=2,
            mfe=0.9,
            mae=1.9,
        ),
        make_trade(
            strategy="trend_pullback",
            regime="trending",
            direction="LONG",
            exit_reason="TIMEOUT",
            entry_time="2026-03-25T09:00:00+00:00",
            pnl_pct=0.6,
            pnl_usd=6.0,
            score=7.1,
            sl_pct=1.2,
            duration_hours=18,
            mfe=1.4,
            mae=0.8,
        ),
    ]

    breakdowns = bt._build_trade_breakdowns(trades)

    assert breakdowns["by_strategy"]["breakout"]["trades"] == 2
    assert breakdowns["by_regime"]["volatile"]["total_pnl_usd"] == 13.0
    assert breakdowns["by_exit_reason"]["TP"]["trades"] == 1
    assert breakdowns["by_exit_reason"]["TIMEOUT"]["avg_duration_hours"] == 18.0
    assert list(breakdowns["by_entry_hour_utc"].keys()) == ["09", "13"]
    assert breakdowns["by_entry_hour_utc"]["13"]["trades"] == 2
    assert breakdowns["by_entry_weekday_utc"]["Tue"]["trades"] == 2
    assert breakdowns["by_entry_weekday_utc"]["Wed"]["trades"] == 1
