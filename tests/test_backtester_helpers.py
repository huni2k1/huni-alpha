from datetime import datetime, timezone

import pytest

from trading_bot import backtester as bt


def make_candle(open_time_ms, close_time_ms=None, **overrides):
    if close_time_ms is None:
        close_time_ms = open_time_ms + 3_600_000
    candle = {
        "open_time": open_time_ms,
        "close_time": close_time_ms,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1_000.0,
    }
    candle.update(overrides)
    return candle


def make_trade(**overrides):
    trade = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "strategy": "trend_pullback",
        "regime": "trending",
        "score": 7.0,
        "entry_time": "2026-04-01T12:00:00+00:00",
        "exit_reason": "TP",
        "pnl_pct": 2.0,
        "pnl_usd": 20.0,
        "sl_pct": 1.0,
        "duration_hours": 6,
        "max_favorable_pct": 3.0,
        "max_adverse_pct": 0.8,
    }
    trade.update(overrides)
    return trade


class DummyResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_validate_candle_completeness_rejects_empty():
    ok, message = bt.validate_candle_completeness("BTCUSDT", [], expected_hours=24)
    assert ok is False
    assert "No candles fetched" in message


def test_validate_candle_completeness_rejects_large_missing_range():
    candles = [make_candle(0), make_candle(3_600_000)]
    ok, message = bt.validate_candle_completeness("BTCUSDT", candles, expected_hours=24)
    assert ok is False
    assert "Incomplete data" in message


def test_validate_candle_completeness_warns_on_minor_gap(monkeypatch):
    warnings = []
    monkeypatch.setattr(bt.log, "warning", warnings.append)
    candles = [
        make_candle(0),
        make_candle(3_600_000),
        make_candle(10_800_000),
    ]

    ok, message = bt.validate_candle_completeness("BTCUSDT", candles, expected_hours=3)

    assert ok is True
    assert "OK" in message
    assert warnings
    assert "gap(s) detected" in warnings[0]


def test_validate_candle_completeness_rejects_huge_gap():
    candles = [
        make_candle(0),
        make_candle(90_000_000),
        make_candle(93_600_000),
    ]

    ok, message = bt.validate_candle_completeness("BTCUSDT", candles, expected_hours=3)

    assert ok is False
    assert "gap > 24h" in message


def test_fetch_klines_historical_retries_then_succeeds(monkeypatch):
    payload = [
        [0, "100", "101", "99", "100.5", "10", 3_599_999, "1005", 0, 0, "500"],
        [3_600_000, "100.5", "102", "100", "101", "11", 7_199_999, "1111", 0, 0, "600"],
    ]
    calls = {"count": 0}

    def fake_get(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary network failure")
        return DummyResponse(payload)

    sleeps = []
    monkeypatch.setattr(bt.SESSION, "get", fake_get)
    monkeypatch.setattr(bt.time, "sleep", lambda seconds: sleeps.append(seconds))

    candles = bt.fetch_klines_historical("BTCUSDT", "1h", 0, 7_200_000, limit=1000, max_retries=2)

    assert calls["count"] == 2
    assert sleeps == [1.0]
    assert len(candles) == 2
    assert candles[0]["quote_asset_volume"] == 1005.0
    assert candles[1]["taker_buy_quote_asset_volume"] == 600.0


def test_fetch_klines_historical_returns_empty_after_failed_retries(monkeypatch):
    monkeypatch.setattr(bt.SESSION, "get", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(bt.time, "sleep", lambda seconds: None)

    candles = bt.fetch_klines_historical("BTCUSDT", "1h", 0, 7_200_000, max_retries=2)

    assert candles == []


def test_fetch_klines_historical_cached_respects_use_cache_false(monkeypatch):
    expected = [make_candle(0)]
    monkeypatch.setattr(bt, "fetch_klines_historical", lambda *args, **kwargs: expected)

    candles = bt.fetch_klines_historical_cached("BTCUSDT", "1h", 0, 3_600_000, use_cache=False)

    assert candles == expected


def test_fetch_klines_historical_cached_refetches_legacy_cache(monkeypatch):
    monkeypatch.setattr(bt.candle_cache, "load_from_cache", lambda *args, **kwargs: [{"close": 100.0}])
    monkeypatch.setattr(bt.candle_cache, "save_to_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(bt, "fetch_klines_historical", lambda *args, **kwargs: [make_candle(0)])

    warnings = []
    monkeypatch.setattr(bt.log, "warning", warnings.append)

    candles = bt.fetch_klines_historical_cached("BTCUSDT", "1h", 0, 3_600_000, use_cache=True)

    assert len(candles) == 1
    assert warnings
    assert "Ignoring legacy cache" in warnings[0]


def test_annualize_return_handles_zero_months_and_total_wipeout():
    assert bt._annualize_return(10.0, 0) == 0.0
    assert bt._annualize_return(-100.0, 12) == -100.0


def test_format_period_label_uses_exact_dates_when_present():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 12, 31, tzinfo=timezone.utc)
    assert bt._format_period_label(12, start, end) == "2025-01-01 to 2025-12-31"
    assert bt._format_period_label(6) == "6 months"


def test_aggregate_trade_stats_empty_and_non_empty():
    empty = bt._aggregate_trade_stats([])
    assert empty["trades"] == 0
    assert empty["profit_factor"] == 0

    stats = bt._aggregate_trade_stats(
        [
            make_trade(pnl_usd=20.0, pnl_pct=2.0, sl_pct=1.0),
            make_trade(pnl_usd=-10.0, pnl_pct=-1.0, sl_pct=1.0, exit_reason="SL"),
        ]
    )

    assert stats["trades"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["profit_factor"] == 2.0
    assert stats["avg_r_multiple"] == 0.5


def test_parse_trade_timestamp_accepts_z_suffix():
    parsed = bt._parse_trade_timestamp("2026-04-01T12:00:00Z")
    assert parsed == datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)


def test_build_trade_breakdowns_falls_back_to_unknown_details():
    trades = [
        make_trade(strategy="", regime="", entry_time="2026-04-02T09:00:00Z", exit_reason="TIMEOUT"),
        make_trade(
            strategy=None,
            regime=None,
            details={"strategy": "breakout", "regime": "volatile"},
            entry_time="2026-04-03T15:00:00Z",
            exit_reason="SL",
            pnl_usd=-5.0,
            pnl_pct=-0.5,
        ),
    ]

    breakdowns = bt._build_trade_breakdowns(trades)

    assert breakdowns["by_strategy"]["unknown"]["trades"] == 1
    assert breakdowns["by_strategy"]["breakout"]["trades"] == 1
    assert breakdowns["by_regime"]["volatile"]["losses"] == 1
    assert list(breakdowns["by_entry_hour_utc"].keys()) == ["09", "15"]


def test_print_summary_renders_extended_sections(capsys):
    results = {
        "config": {
            "months": 12,
            "period_label": "2025-01-01 to 2025-12-31",
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "risk_pct": 1.5,
            "threshold_entry": 6.0,
            "trend_threshold": 7.0,
            "fee_pct": 0.06,
            "slippage_pct": 0.05,
            "trailing_stop": True,
            "max_positions": 3,
            "cooldown_candles": 24,
            "use_next_open": True,
            "starting_account": 1000.0,
        },
        "summary": {
            "final_equity": 1125.0,
            "total_pnl_usd": 125.0,
            "total_return_pct": 12.5,
            "max_drawdown_usd": 75.0,
            "max_drawdown_pct": 6.7,
            "total_fees_usd": 8.5,
            "total_trades": 12,
            "win_rate": 58.3,
            "wins": 7,
            "losses": 5,
            "profit_factor": float("inf"),
            "avg_ev_per_trade_pct": 1.2,
            "avg_ev_per_trade_usd": 10.4,
            "avg_win_pct": 2.4,
            "avg_loss_pct": -1.1,
            "avg_win_usd": 22.0,
            "avg_loss_usd": -9.5,
            "payoff_ratio": 2.32,
            "avg_r_multiple": 0.82,
            "avg_win_r": 1.9,
            "avg_loss_r": -0.8,
            "avg_mfe_pct": 3.1,
            "avg_mae_pct": 1.4,
            "best_trade_pct": 5.6,
            "worst_trade_pct": -2.2,
            "max_consecutive_wins": 4,
            "max_consecutive_losses": 2,
            "tp_exits": 5,
            "sl_exits": 4,
            "trail_sl_exits": 1,
            "timeout_exits": 2,
            "long_trades": 7,
            "long_win_rate": 57.1,
            "short_trades": 5,
            "short_win_rate": 60.0,
            "avg_trades_per_month": 1.0,
            "annualized_return_pct": 12.5,
            "positive_months": 8,
            "months_tested": 12,
            "consistency_pct": 66.7,
            "avg_monthly_return_pct": 1.0,
            "best_month_return_pct": 5.0,
            "worst_month_return_pct": -2.0,
            "monthly_volatility_pct": 2.4,
            "downside_volatility_pct": 1.1,
            "sharpe_ratio": 1.8,
            "sortino_ratio": 2.5,
            "calmar_ratio": 1.9,
            "recovery_factor": 1.7,
            "avg_duration_hours": 16.2,
            "avg_duration_tp_hours": 10.0,
            "avg_duration_sl_hours": 7.5,
            "avg_duration_long_hours": 14.0,
            "avg_duration_short_hours": 19.0,
        },
        "monthly": [
            {"month": "2025-01", "trades": 3, "win_rate": 67, "pnl_usd": 40.0, "monthly_return_pct": 4.0},
            {"month": "2025-02", "trades": 2, "win_rate": 50, "pnl_usd": -10.0, "monthly_return_pct": -1.0},
        ],
        "rolling_returns": {
            "1m": {"pnl_usd": 25.0, "return_pct": 2.5, "months_available": 1, "months_requested": 1},
            "3m": {"pnl_usd": 50.0, "return_pct": 5.0, "months_available": 3, "months_requested": 3},
        },
        "by_score_band": {
            "6.0-6.9": {"trades": 4, "win_rate": 50, "avg_pnl_pct": 0.5, "pnl_usd": 8.0},
        },
        "by_strategy_direction": {
            "trend_pullback_LONG": {"trades": 5, "win_rate": 60, "profit_factor": 1.8, "avg_pnl_pct": 0.9, "total_pnl_usd": 35.0},
        },
        "by_exit_reason": {
            "TP": {"trades": 5, "win_rate": 100, "avg_pnl_pct": 2.1, "avg_duration_hours": 10.5, "total_pnl_usd": 55.0},
        },
        "by_symbol": {
            "BTCUSDT": {"total_trades": 6, "win_rate": 67, "total_pnl_usd": 40.0, "best_trade": 4.0, "worst_trade": -1.5},
        },
        "by_regime": {
            "bull": {"trades": 5, "win_rate": 60, "profit_factor": 1.7, "total_pnl_usd": 30.0},
        },
        "by_entry_weekday_utc": {
            "Mon": {"trades": 4, "win_rate": 50, "avg_pnl_pct": 0.4, "total_pnl_usd": 5.0},
        },
        "by_entry_hour_utc": {
            "09": {"trades": 6, "win_rate": 66, "avg_pnl_pct": 0.7, "total_pnl_usd": 12.0},
            "13": {"trades": 3, "win_rate": 33, "avg_pnl_pct": -0.2, "total_pnl_usd": -2.0},
        },
        "rejection_counts": {"No matching rule": 7, "Score below threshold": 5},
    }

    bt.print_summary(results)
    output = capsys.readouterr().out

    assert "Rolling Returns Summary" in output
    assert "Score Band" in output
    assert "trend_pullback_LONG" in output
    assert "Best Entry Hours UTC" in output
    assert "Top Rejection Reasons" in output
    assert "INF" in output
