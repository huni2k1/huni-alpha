from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from trading_bot import backtester as bt
from trading_bot.core.types import Signal


def make_candle_dict(open_, high, low, close, volume=1000, open_time_ms=None, close_time_ms=None):
    if open_time_ms is None:
        open_time_ms = 1000000000000
    if close_time_ms is None:
        close_time_ms = open_time_ms + 3600000
    return {
        "open_time": open_time_ms,
        "close_time": close_time_ms,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def make_flat_candles_dict(n, price=100.0, volume=1000):
    candles = []
    for i in range(n):
        open_time_ms = 1000000000000 + (i * 3600000)
        close_time_ms = open_time_ms + 3600000 - 1
        candles.append(
            make_candle_dict(
                open_=price,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                volume=volume,
                open_time_ms=open_time_ms,
                close_time_ms=close_time_ms,
            )
        )
    return candles


def test_next_open_entry_uses_actual_entry_candle_time_and_index():
    candles = make_flat_candles_dict(4082, price=100.0)
    entry_idx = 4081
    candles[entry_idx]["open"] = 123.0
    candles[entry_idx]["high"] = 123.5
    candles[entry_idx]["low"] = 122.5
    candles[entry_idx]["close"] = 123.0

    signal = Signal(
        symbol="BTCUSDT", direction="LONG", score=7.0,
        entry_price=100.0, tp=110.0, sl=95.0,
        tp_pct=10.0, sl_pct=5.0, atr=1.0,
        sl_atr_mult=1.5, rr_ratio=2.0,
        strategy="trend_pullback", regime="trending",
        details={"strategy": "trend_pullback", "regime": "trending"},
    )

    with patch.object(bt, "fetch_klines_historical_cached", return_value=candles), \
         patch.object(bt, "validate_candle_completeness", return_value=(True, "ok")), \
         patch.object(bt, "precompute_indicators_for_all_candles", return_value={}), \
         patch.object(bt, "generate_signal", side_effect=[signal]):
        results = bt.run_backtest(
            symbols=["BTCUSDT"],
            months=1,
            account=1000.0,
            fee_pct=0.0,
            slippage_pct=0.0,
            fixed_size=100.0,
            max_positions=1,
            cooldown_candles=1,
            use_next_open=True,
            kelly_sizing=False,
        )

    assert len(results["trades"]) == 1

    trade = results["trades"][0]
    expected_entry_time = datetime.fromtimestamp(
        candles[entry_idx]["open_time"] / 1000, tz=timezone.utc
    ).isoformat()

    assert trade["entry_price"] == candles[entry_idx]["open"]
    assert trade["entry_time"] == expected_entry_time
    assert trade["duration_hours"] == 0


def test_next_open_reanchors_tp_sl_and_caps_position_size():
    candles = make_flat_candles_dict(4082, price=100.0)
    entry_idx = 4081
    candles[entry_idx]["open"] = 120.0
    candles[entry_idx]["high"] = 120.5
    candles[entry_idx]["low"] = 119.5
    candles[entry_idx]["close"] = 120.0

    # Recipe: atr=1, sl_atr_mult=1.0, rr_ratio=10.0 → sl_distance=1, tp_distance=10.
    # Applied at the actual fill (entry=120 next-open) → tp=130, sl=119.
    signal = Signal(
        symbol="BTCUSDT", direction="LONG", score=7.0,
        entry_price=100.0, tp=110.0, sl=99.0,
        tp_pct=10.0, sl_pct=1.0,
        atr=1.0, sl_atr_mult=1.0, rr_ratio=10.0,
        strategy="trend_pullback", regime="trending",
        details={"strategy": "trend_pullback", "regime": "trending", "rsi": {}},
    )

    with patch.object(bt, "fetch_klines_historical_cached", return_value=candles), \
         patch.object(bt, "validate_candle_completeness", return_value=(True, "ok")), \
         patch.object(bt, "generate_signal", side_effect=[signal]):
        results = bt.run_backtest(
            symbols=["BTCUSDT"],
            months=1,
            account=5000.0,
            fee_pct=0.0,
            slippage_pct=0.0,
            fixed_size=0.0,
            max_positions=2,
            cooldown_candles=1,
            use_next_open=True,
            kelly_sizing=False,
        )

    assert len(results["trades"]) == 1

    trade = results["trades"][0]
    assert trade["entry_price"] == 120.0
    assert trade["tp_price"] == 130.0
    assert trade["sl_price"] == 119.0
    assert trade["position_size"] == pytest.approx(2500.0, abs=0.01)


def test_format_period_label_uses_exact_dates_when_provided():
    label = bt._format_period_label(
        6,
        start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2025, 12, 31, tzinfo=timezone.utc),
    )

    assert label == "2025-01-01 to 2025-12-31"


def test_print_summary_prefers_period_label_for_exact_date_runs(capsys):
    results = {
        "config": {
            "symbols": ["ETHUSDT", "AVAXUSDT"],
            "months": 6,
            "period_label": "2025-01-01 to 2025-12-31",
            "starting_account": 1000.0,
            "risk_pct": 1.5,
            "fee_pct": 0.06,
            "slippage_pct": 0.05,
            "trailing_stop": False,
            "threshold_entry": 7.0,
            "trend_threshold": 7.0,
            "max_positions": 3,
            "cooldown_candles": 48,
            "use_next_open": True,
        },
        "summary": {
            "final_equity": 1080.25,
            "total_pnl_usd": 80.25,
            "total_return_pct": 8.03,
            "max_drawdown_usd": 29.72,
            "max_drawdown_pct": 2.9,
            "total_fees_usd": 8.75,
            "total_trades": 33,
            "win_rate": 42.4,
            "wins": 14,
            "losses": 19,
            "profit_factor": 1.58,
            "avg_ev_per_trade_pct": 0.44,
            "avg_ev_per_trade_usd": 2.43,
            "avg_win_pct": 3.41,
            "avg_loss_pct": -1.75,
            "avg_win_usd": 15.61,
            "avg_loss_usd": 7.28,
            "payoff_ratio": 2.14,
            "avg_r_multiple": 0.17,
            "avg_win_r": 1.91,
            "avg_loss_r": -1.11,
            "avg_mfe_pct": 2.24,
            "avg_mae_pct": 1.52,
            "best_trade_pct": 5.27,
            "worst_trade_pct": -3.22,
            "max_consecutive_wins": 3,
            "max_consecutive_losses": 3,
            "tp_exits": 14,
            "sl_exits": 19,
            "trail_sl_exits": 0,
            "timeout_exits": 0,
            "long_trades": 0,
            "long_win_rate": 0.0,
            "short_trades": 33,
            "short_win_rate": 42.4,
            "avg_trades_per_month": 2.8,
            "annualized_return_pct": 8.02,
            "positive_months": 6,
            "months_tested": 12,
            "consistency_pct": 50.0,
            "avg_monthly_return_pct": 0.66,
            "best_month_return_pct": 3.02,
            "worst_month_return_pct": -1.29,
            "monthly_volatility_pct": 1.72,
            "downside_volatility_pct": 0.22,
            "sharpe_ratio": 1.33,
            "sortino_ratio": 10.52,
            "calmar_ratio": 2.78,
            "recovery_factor": 2.78,
            "avg_duration_hours": 12.3,
            "avg_duration_tp_hours": 14.1,
            "avg_duration_sl_hours": 11.0,
            "avg_duration_long_hours": 0.0,
            "avg_duration_short_hours": 12.3,
        },
        "monthly": [],
        "rolling_returns": {},
        "by_strategy_direction": {},
        "by_exit_reason": {},
        "by_symbol": {},
        "by_regime": {},
        "by_entry_weekday_utc": {},
        "by_entry_hour_utc": {},
        "rejection_counts": {},
    }

    bt.print_summary(results)
    output = capsys.readouterr().out

    assert "2025-01-01 to 2025-12-31 | ETH, AVAX" in output
    assert "6 months | ETH, AVAX" not in output
