import copy
from datetime import datetime, timedelta, timezone

import pytest

from trading_bot import scanner, trader


class _FakeResponse:
    def raise_for_status(self):
        return None


class _FakeDryRunClient:
    testnet = False

    def ensure_one_way_mode(self):
        return None


def _patch_trader_startup(monkeypatch, cfg, state):
    monkeypatch.setattr(trader, "_acquire_pid_lock", lambda: True)
    monkeypatch.setattr(trader, "_release_pid_lock", lambda: None)
    monkeypatch.setattr(trader.atexit, "register", lambda fn: None)
    monkeypatch.setattr(trader.signal, "signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(trader, "_load_config", lambda: cfg)
    monkeypatch.setattr(trader, "BinanceClient", _FakeDryRunClient)
    monkeypatch.setattr(trader, "load_state", lambda: state)
    monkeypatch.setattr(trader, "_check_circuit_breaker", lambda *args, **kwargs: None)
    monkeypatch.setattr(trader, "_circuit_breaker_active", lambda *args, **kwargs: False)


def test_scanner_send_telegram_logs_debug_on_success(monkeypatch):
    debug_logs = []
    info_logs = []

    monkeypatch.setattr(scanner, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(scanner, "TELEGRAM_CHAT", "chat")
    monkeypatch.setattr(scanner.requests, "post", lambda *args, **kwargs: _FakeResponse())
    monkeypatch.setattr(scanner.log, "debug", lambda msg: debug_logs.append(msg))
    monkeypatch.setattr(scanner.log, "info", lambda msg: info_logs.append(msg))

    scanner.send_telegram("hello")

    assert debug_logs == ["Telegram message sent."]
    assert info_logs == []


def test_scanner_summary_falls_back_to_active_engine(monkeypatch):
    info_logs = []
    sleep_calls = {"count": 0}

    monkeypatch.setattr(scanner, "DEFAULT_SIGNAL_ENGINE", "combined")
    monkeypatch.setattr(scanner, "SYMBOLS", ["BTCUSDT"])
    monkeypatch.setattr(scanner, "load_state", lambda: {})
    monkeypatch.setattr(scanner, "send_telegram", lambda msg: None)
    monkeypatch.setattr(scanner.log, "info", lambda msg: info_logs.append(msg))

    def _fake_scan_symbol(_symbol, _state):
        scanner._cycle_results.append({
            "coin": "BTC",
            "price": 100.0,
            "dir": "SHORT",
            "tech": 0.0,
            "total": 1.0,
            "strategy": "rule_wide",
            "filter_reason": None,
        })

    def _fake_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] >= 2:
            raise SystemExit(0)

    monkeypatch.setattr(scanner, "scan_symbol", _fake_scan_symbol)
    monkeypatch.setattr(scanner.time, "sleep", _fake_sleep)

    with pytest.raises(SystemExit):
        scanner.main()

    assert any("Scanner mode summary: engine=combined" in line for line in info_logs)
    assert any("combined" in line and "rule_wide" in line for line in info_logs)


def test_execute_entry_telegram_includes_audit_context(monkeypatch):
    sent = []
    monkeypatch.setattr(trader, "send_telegram", lambda msg: sent.append(msg))

    state = copy.deepcopy(trader._EMPTY_STATE)
    signal = {
        "symbol": "ETHUSDT",
        "direction": "LONG",
        "entry_price": 100.0,
        "tp": 104.0,
        "sl": 98.0,
        "tp_pct": 4.0,
        "sl_pct": 2.0,
        "score": 7.0,
        "strategy": "breakout",
        "signal_engine": "combined",
        "selected_source": "statistical",
        "statistical_setup": "wide_short_rsi_below_28",
        "market_regime": "bear",
    }
    cfg = {
        "risk_pct": 1.5,
        "max_positions": 3,
        "signal_engine": "combined",
    }

    opened = trader.execute_entry(
        signal,
        state,
        client=object(),
        cfg=cfg,
        dry_run=True,
        equity=1000.0,
    )

    assert opened is True
    assert sent
    assert "Engine: <code>combined</code>" in sent[0]
    assert "Source: <code>statistical</code>" in sent[0]
    assert "Setup: <code>wide_short_rsi_below_28</code>" in sent[0]
    assert "Regime: <code>bear</code>" in sent[0]
    assert "Protection: <code>armed</code>" in sent[0]


def test_monitor_positions_close_telegram_includes_audit_context(monkeypatch):
    sent = []
    monkeypatch.setattr(trader, "send_telegram", lambda msg: sent.append(msg))

    state = copy.deepcopy(trader._EMPTY_STATE)
    state["positions"] = {
        "BTCUSDT": {
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "entry_price": 100.0,
            "entry_time": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "quantity": 1.0,
            "position_size_usdt": 100.0,
            "score": 6.5,
            "strategy": "breakout",
            "signal_engine": "combined",
            "selected_source": "technical",
            "setup_name": "breakout",
            "market_regime": "bull",
            "protection_status": "armed",
            "tp_order_id": "DRY_TP",
            "sl_order_id": "DRY_SL",
        }
    }

    trader.monitor_positions(
        state,
        client=object(),
        cfg={"timeout_hours": 1},
        dry_run=True,
    )

    assert "BTCUSDT" not in state["positions"]
    assert sent
    assert "Engine: <code>combined</code>" in sent[0]
    assert "Source: <code>technical</code>" in sent[0]
    assert "Setup: <code>breakout</code>" in sent[0]
    assert "Regime: <code>bull</code>" in sent[0]
    assert "Protection: <code>armed</code>" in sent[0]


def test_live_trader_logs_cycle_rejection_summary(monkeypatch):
    cfg = {
        "dry_run": True,
        "signal_engine": "combined",
        "risk_pct": 1.5,
        "max_positions": 3,
        "scan_interval": 300,
        "cooldown_hours": 24,
        "timeout_hours": 180,
        "max_drawdown_pct": 25.0,
        "circuit_break_hours": 168,
        "window_size": 220,
    }
    state = copy.deepcopy(trader._EMPTY_STATE)
    _patch_trader_startup(monkeypatch, cfg, state)
    monkeypatch.setattr(trader, "get_app_version", lambda: "test123")
    monkeypatch.setattr(trader, "SYMBOLS", ["BTCUSDT"])
    monkeypatch.setattr(trader, "send_telegram", lambda msg: None)
    monkeypatch.setattr(trader, "monitor_positions", lambda *args, **kwargs: None)

    info_logs = []
    monkeypatch.setattr(trader.log, "info", lambda msg: info_logs.append(msg))

    def _fake_fetch(symbol, interval, limit, use_cache=False):
        assert interval == "1h"
        return [[1.0, 1.0, 1.0, 100.0, 1.0]] * limit

    def _fake_generate_signal(*args, **kwargs):
        scanner._last_rejection_reason["BTCUSDT"] = "Asia session (UTC 1)"
        return None

    sleep_calls = {"count": 0}

    def _fake_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] >= 2:
            raise SystemExit(0)

    monkeypatch.setattr(trader, "fetch_klines_cached", _fake_fetch)
    monkeypatch.setattr(trader, "generate_signal", _fake_generate_signal)
    monkeypatch.setattr(trader.time, "sleep", _fake_sleep)

    with pytest.raises(SystemExit):
        trader.main()

    assert any("Rejection summary: Asia session (UTC 1)=1" in line for line in info_logs)
