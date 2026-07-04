import copy
import os
from pathlib import Path

import pytest

from trading_bot import trader


class _FakeLiveClient:
    testnet = False

    def ensure_one_way_mode(self):
        return None

    def get_usdt_balance(self):
        return None

    def get_usdt_equity(self):
        return None


class _FakeDryRunClient:
    testnet = False

    def ensure_one_way_mode(self):
        return None


def _patch_common_startup(monkeypatch, cfg, state, client_cls):
    monkeypatch.setattr(trader, "_acquire_pid_lock", lambda: True)
    monkeypatch.setattr(trader, "_release_pid_lock", lambda: None)
    monkeypatch.setattr(trader.atexit, "register", lambda fn: None)
    monkeypatch.setattr(trader.signal, "signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(trader, "_load_config", lambda: cfg)
    monkeypatch.setattr(trader, "BinanceClient", client_cls)
    monkeypatch.setattr(trader, "load_state", lambda: state)
    monkeypatch.setattr(trader, "_check_circuit_breaker", lambda *args, **kwargs: None)
    monkeypatch.setattr(trader, "_circuit_breaker_active", lambda *args, **kwargs: False)


def test_live_trader_startup_logs_version_and_reconcile_on_balance_failure(monkeypatch):
    cfg = {
        "dry_run": False,
        "signal_engine": "combined",
        "risk_pct": 1.5,
        "max_positions": 3,
        "scan_interval": 300,
        "cooldown_hours": 24,
        "timeout_hours": 180,
        "max_drawdown_pct": 25.0,
        "circuit_break_hours": 168,
        "window_size": 1000,
    }
    state = copy.deepcopy(trader._EMPTY_STATE)
    _patch_common_startup(monkeypatch, cfg, state, _FakeLiveClient)
    monkeypatch.setattr(trader, "get_app_version", lambda: "ver1234")

    info_logs = []
    error_logs = []
    telegram_messages = []
    reconcile_calls = []

    monkeypatch.setattr(trader.log, "info", lambda msg: info_logs.append(msg))
    monkeypatch.setattr(trader.log, "error", lambda msg, *args, **kwargs: error_logs.append(msg))
    monkeypatch.setattr(trader, "send_telegram", lambda msg: telegram_messages.append(msg))
    monkeypatch.setattr(trader, "reconcile_with_binance", lambda *args, **kwargs: reconcile_calls.append(True))

    def _stop_sleep(_seconds):
        raise SystemExit(0)

    monkeypatch.setattr(trader.time, "sleep", _stop_sleep)

    with pytest.raises(SystemExit):
        trader.main()

    assert any("Version:        ver1234" in line for line in info_logs)
    assert any("Failed to get balance from Binance, skipping entry scan" in line for line in error_logs)
    assert reconcile_calls == [True]
    assert telegram_messages
    # Compact startup: version as inline code, config on one line
    assert "<code>ver1234</code>" in telegram_messages[0]
    assert "Risk 1.5% / trade" in telegram_messages[0]
    assert "Timeout 180h" in telegram_messages[0]


def test_live_trader_skips_scan_when_already_at_max_positions(monkeypatch):
    cfg = {
        "dry_run": True,
        "signal_engine": "ta_score",
        "risk_pct": 1.5,
        "max_positions": 3,
        "scan_interval": 300,
        "cooldown_hours": 24,
        "timeout_hours": 180,
        "max_drawdown_pct": 25.0,
        "circuit_break_hours": 168,
        "window_size": 1000,
    }
    state = {
        **copy.deepcopy(trader._EMPTY_STATE),
        "positions": {
            "BTCUSDT": {"symbol": "BTCUSDT"},
            "ETHUSDT": {"symbol": "ETHUSDT"},
            "SOLUSDT": {"symbol": "SOLUSDT"},
        },
    }
    _patch_common_startup(monkeypatch, cfg, state, _FakeDryRunClient)
    monkeypatch.setattr(trader, "get_app_version", lambda: "dry1234")

    info_logs = []
    telegram_messages = []

    monkeypatch.setattr(trader.log, "info", lambda msg: info_logs.append(msg))
    monkeypatch.setattr(trader, "send_telegram", lambda msg: telegram_messages.append(msg))
    monkeypatch.setattr(trader, "monitor_positions", lambda *args, **kwargs: None)
    monkeypatch.setattr(trader, "fetch_klines_cached", lambda *args, **kwargs: pytest.fail("scan should be skipped at max positions"))
    monkeypatch.setattr(trader, "generate_signal", lambda *args, **kwargs: pytest.fail("scan should be skipped at max positions"))

    def _stop_sleep(_seconds):
        raise SystemExit(0)

    monkeypatch.setattr(trader.time, "sleep", _stop_sleep)

    with pytest.raises(SystemExit):
        trader.main()

    assert any("At max positions (3/3), skipping scan" in line for line in info_logs)
    assert telegram_messages


def test_live_trader_skips_symbol_in_cooldown(monkeypatch):
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
    state = {
        **copy.deepcopy(trader._EMPTY_STATE),
        "last_signal": {
            "ETHUSDT": trader.datetime.now(trader.timezone.utc).isoformat(),
        },
    }
    _patch_common_startup(monkeypatch, cfg, state, _FakeDryRunClient)

    info_logs = []
    monkeypatch.setattr(trader, "get_app_version", lambda: "cool123")
    monkeypatch.setattr(trader, "SYMBOLS", ["ETHUSDT"])
    monkeypatch.setattr(trader, "send_telegram", lambda msg: None)
    monkeypatch.setattr(trader, "monitor_positions", lambda *args, **kwargs: None)
    monkeypatch.setattr(trader, "classify_current_regime", lambda candles: "bull")
    monkeypatch.setattr(
        trader,
        "fetch_klines_cached",
        lambda symbol, interval, limit, use_cache=False, **kwargs: [[1.0, 1.0, 1.0, 100.0, 1.0]] * limit,
    )
    monkeypatch.setattr(trader, "generate_signal", lambda *args, **kwargs: pytest.fail("cooldown symbol should not generate"))
    monkeypatch.setattr(trader.log, "info", lambda msg: info_logs.append(msg))

    def _stop_sleep(_seconds):
        raise SystemExit(0)

    monkeypatch.setattr(trader.time, "sleep", _stop_sleep)

    with pytest.raises(SystemExit):
        trader.main()

    assert any("ETHUSDT: in cooldown, skip" in line for line in info_logs)


def test_live_trader_warns_on_insufficient_candle_data(monkeypatch):
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
    _patch_common_startup(monkeypatch, cfg, state, _FakeDryRunClient)

    warnings = []
    sleep_calls = {"count": 0}
    monkeypatch.setattr(trader, "get_app_version", lambda: "data123")
    monkeypatch.setattr(trader, "SYMBOLS", ["ETHUSDT"])
    monkeypatch.setattr(trader, "send_telegram", lambda msg: None)
    monkeypatch.setattr(trader, "monitor_positions", lambda *args, **kwargs: None)
    monkeypatch.setattr(trader, "classify_current_regime", lambda candles: "bull")

    def _fake_fetch(symbol, interval, limit, use_cache=False, **kwargs):
        if symbol == "BTCUSDT":
            return [[1.0, 1.0, 1.0, 100.0, 1.0]] * limit
        return [[1.0, 1.0, 1.0, 100.0, 1.0]] * 100

    monkeypatch.setattr(trader, "fetch_klines_cached", _fake_fetch)
    monkeypatch.setattr(trader, "generate_signal", lambda *args, **kwargs: pytest.fail("insufficient data should skip generate_signal"))
    monkeypatch.setattr(trader.log, "warning", lambda msg: warnings.append(msg))

    def _fake_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] >= 2:
            raise SystemExit(0)

    monkeypatch.setattr(trader.time, "sleep", _fake_sleep)

    with pytest.raises(SystemExit):
        trader.main()

    assert any("insufficient candle data (100), skip" in line for line in warnings)


def test_live_trader_logs_score_below_threshold_summary(monkeypatch):
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
    _patch_common_startup(monkeypatch, cfg, state, _FakeDryRunClient)

    info_logs = []
    sleep_calls = {"count": 0}
    monkeypatch.setattr(trader, "get_app_version", lambda: "score123")
    monkeypatch.setattr(trader, "SYMBOLS", ["ETHUSDT"])
    monkeypatch.setattr(trader, "send_telegram", lambda msg: None)
    monkeypatch.setattr(trader, "monitor_positions", lambda *args, **kwargs: None)
    monkeypatch.setattr(trader, "classify_current_regime", lambda candles: "bull")
    monkeypatch.setattr(
        trader,
        "fetch_klines_cached",
        lambda symbol, interval, limit, use_cache=False, **kwargs: [[1.0, 1.0, 1.0, 100.0, 1.0]] * limit,
    )
    from trading_bot.core.types import Signal
    _fake_signal = Signal(
        symbol="ETHUSDT", direction="LONG", score=4.5,
        entry_price=100.0, tp=103.0, sl=98.0,
        tp_pct=3.0, sl_pct=2.0, atr=1.0, sl_atr_mult=1.5, rr_ratio=1.5,
        long_score=4.5, short_score=0.0, technical_score=4.5,
        strategy="trend_pullback", signal_engine="combined",
        details={"rsi": {"1h": 55.0}, "adx": 19.0, "regime": "weak_trend"},
    )
    monkeypatch.setattr(trader, "generate_signal", lambda *args, **kwargs: _fake_signal)
    monkeypatch.setattr(trader.log, "info", lambda msg: info_logs.append(msg))

    def _fake_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] >= 2:
            raise SystemExit(0)

    monkeypatch.setattr(trader.time, "sleep", _fake_sleep)

    with pytest.raises(SystemExit):
        trader.main()

    assert any("score 4.50 < 7" in line for line in info_logs)
    assert any("Rejection summary: Score below threshold=1" in line for line in info_logs)


def test_service_file_wires_runtime_env_and_exec_path():
    service_path = Path(__file__).resolve().parents[1] / "deploy" / "huni-alpha.service"
    content = service_path.read_text()

    assert "EnvironmentFile=-/opt/huni-alpha/deploy/runtime.env" in content
    assert "Environment=PYTHONPATH=/opt/huni-alpha/src" in content
    assert "ExecStart=/opt/huni-alpha/.venv/bin/python -u -c 'from trading_bot import trader; trader.main()'" in content
    assert "Restart=always" in content


def test_deploy_script_writes_version_and_restarts_service():
    deploy_path = Path(__file__).resolve().parents[1] / "deploy" / "deploy.sh"
    content = deploy_path.read_text()

    assert 'APP_VERSION="${APP_VERSION:-$(git rev-parse --short HEAD)}"' in content
    assert 'printf "APP_VERSION=%s\\n" "$APP_VERSION" > "$RUNTIME_ENV_FILE"' in content
    assert 'cp "$SERVICE_FILE_SRC" "$SERVICE_FILE_DST"' in content
    assert 'systemctl daemon-reload' in content
    assert 'systemctl restart "$SERVICE_NAME"' in content
    assert 'systemctl status "$SERVICE_NAME" --no-pager' in content
