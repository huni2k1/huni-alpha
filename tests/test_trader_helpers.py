import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_bot import trader
from trading_bot.core.types import Signal


def _make_signal(**overrides):
    """Build a Signal with sensible defaults for tests."""
    defaults = dict(
        symbol="BTCUSDT", direction="LONG", score=7.0,
        entry_price=100.0, tp=104.0, sl=98.0,
        tp_pct=4.0, sl_pct=2.0,
        atr=1.0, sl_atr_mult=1.5, rr_ratio=2.0,
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _fresh_state():
    state = copy.deepcopy(trader._EMPTY_STATE)
    state["positions"] = {}
    state["last_signal"] = {}
    state["trade_log"] = []
    return state


class _AlgoRecoveryClient:
    def __init__(self, symbol_info=None, algo_orders=None):
        self._symbol_info = symbol_info or {"price_precision": 2}
        self._algo_orders = algo_orders or []

    def get_symbol_info(self, symbol):
        return self._symbol_info

    def get_open_algo_orders(self, symbol):
        return list(self._algo_orders)


class _MonitorClient:
    def __init__(self):
        self.statuses = {}
        self.fill_prices = {}
        self.cancelled = []
        self.closed = []
        self.cancel_all = []

    def get_order_status(self, symbol, order_id):
        return self.statuses.get(order_id)

    def get_order_fill_price(self, symbol, order_id):
        return self.fill_prices.get(order_id)

    def cancel_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))
        return True

    def place_market_close(self, symbol, close_side, quantity):
        self.closed.append((symbol, close_side, quantity))
        return None

    def cancel_all_orders(self, symbol):
        self.cancel_all.append(symbol)
        return True


class _EntryClient:
    def __init__(self, *, symbol_info=None, market_fill=None, tp_order_id="algo:tp1", sl_order_id="algo:sl1", market_close=None):
        self.symbol_info = symbol_info or {
            "qty_precision": 3,
            "price_precision": 2,
            "min_qty": 0.001,
            "min_notional": 5.0,
        }
        self.market_fill = market_fill
        self.tp_order_id = tp_order_id
        self.sl_order_id = sl_order_id
        self.market_close = market_close
        self.set_leverage_calls = []
        self.cancel_calls = []
        self.close_calls = []

    def get_symbol_info(self, symbol):
        return self.symbol_info

    def set_leverage(self, symbol, leverage):
        self.set_leverage_calls.append((symbol, leverage))
        return True

    def place_market_order(self, symbol, side, quantity):
        return self.market_fill

    def place_tp_order(self, symbol, close_side, tp_price, price_precision):
        return self.tp_order_id

    def place_sl_order(self, symbol, close_side, sl_price, price_precision):
        return self.sl_order_id

    def cancel_order(self, symbol, order_id):
        self.cancel_calls.append((symbol, order_id))
        return True

    def place_market_close(self, symbol, close_side, quantity):
        self.close_calls.append((symbol, close_side, quantity))
        return self.market_close


def test_get_app_version_prefers_env(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "env123")
    assert trader.get_app_version() == "env123"


def test_get_app_version_falls_back_to_git(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setattr(trader.subprocess, "check_output", lambda *args, **kwargs: "abc123\n")
    assert trader.get_app_version() == "abc123"


def test_get_app_version_returns_unknown_on_git_failure(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setattr(trader.subprocess, "check_output", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no git")))
    assert trader.get_app_version() == "unknown"


def test_load_config_reads_file_and_env_overrides(tmp_path, monkeypatch):
    cfg_path = tmp_path / "trader.json"
    cfg_path.write_text(json.dumps({
        "DRY_RUN": False,
        "SIGNAL_MODEL": "technical",
        "RISK_PER_TRADE_PCT": 2.5,
        "MAX_POSITIONS": 5,
        "SCAN_INTERVAL_SEC": 120,
        "COOLDOWN_HOURS": 48,
        "TIMEOUT_HOURS": 100,
        "MAX_DRAWDOWN_PCT": 12.5,
        "CIRCUIT_BREAK_HOURS": 24,
        "WINDOW_SIZE": 500,
    }))
    monkeypatch.setattr(trader, "_TRADER_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("SIGNAL_MODEL", "combined")
    monkeypatch.setenv("DRY_RUN", "false")

    cfg = trader._load_config()

    assert cfg["dry_run"] is False
    assert cfg["signal_engine"] == "combined"
    assert cfg["risk_pct"] == 2.5
    assert cfg["max_positions"] == 5
    assert cfg["scan_interval"] == 120
    assert cfg["window_size"] == 500


def test_load_state_returns_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "_STATE_FILE", str(tmp_path / "missing.json"))
    state = trader.load_state()
    assert state["positions"] == {}
    assert state["trade_log"] == []


def test_load_state_fills_missing_keys(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"positions": {"BTCUSDT": {"symbol": "BTCUSDT"}}}))
    monkeypatch.setattr(trader, "_STATE_FILE", str(path))

    state = trader.load_state()

    assert "last_signal" in state
    assert "trade_log" in state
    assert state["positions"]["BTCUSDT"]["symbol"] == "BTCUSDT"


def test_save_state_writes_json(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(trader, "_STATE_FILE", str(path))
    trader.save_state({"positions": {"BTCUSDT": {"symbol": "BTCUSDT"}}})
    assert json.loads(path.read_text())["positions"]["BTCUSDT"]["symbol"] == "BTCUSDT"


def test_write_position_state_rounds_and_saves(monkeypatch):
    state = _fresh_state()
    saves = []
    monkeypatch.setattr(trader, "save_state", lambda s: saves.append(copy.deepcopy(s)))

    trader._write_position_state(
        state,
        symbol="BTCUSDT",
        direction="LONG",
        fill_price=100.1234,
        quantity=0.25,
        score=7.2,
        strategy="breakout",
        signal_engine="combined",
        price_precision=2,
        tp_price=105.678,
        sl_price=98.111,
        tp_order_id="tp1",
        sl_order_id="sl1",
        protection_status="armed",
        selected_source="technical",
        setup_name="breakout",
        market_regime="bull",
    )

    pos = state["positions"]["BTCUSDT"]
    assert pos["tp_price"] == 105.68
    assert pos["sl_price"] == 98.11
    assert pos["selected_source"] == "technical"
    assert pos["setup_name"] == "breakout"
    assert pos["market_regime"] == "bull"
    assert saves


def test_signal_audit_context_prefers_explicit_fields():
    signal = _make_signal(
        signal_engine="combined",
        selected_source="rulebook",
        setup_name="wide_short",
        market_regime="bear",
    )
    assert trader._signal_audit_context(signal) == {
        "engine": "combined",
        "source": "rulebook",
        "setup_name": "wide_short",
        "market_regime": "bear",
    }


def test_signal_audit_context_infers_defaults():
    signal = _make_signal(
        strategy="trend_pullback",
        details={"regime": "weak_trend"},
        regime="weak_trend",
    )
    ctx = trader._signal_audit_context(signal, "ta_score")
    assert ctx["engine"] == "ta_score"
    assert ctx["source"] == "technical"
    assert ctx["setup_name"] == "trend_pullback"
    assert ctx["market_regime"] == "weak_trend"


def test_format_rejection_summary_orders_by_count_then_name():
    summary = trader._format_rejection_summary({"B": 1, "A": 2, "C": 2})
    assert summary == "A=2 | C=2 | B=1"


def test_bump_rejection_count_increments():
    counts = {}
    trader._bump_rejection_count(counts, "No Signal")
    trader._bump_rejection_count(counts, "No Signal")
    assert counts == {"No Signal": 2}


def test_recover_position_state_from_binance_restores_protection():
    state = _fresh_state()
    client = _AlgoRecoveryClient(
        algo_orders=[
            {"side": "SELL", "type": "TAKE_PROFIT_MARKET", "triggerPrice": "105.1", "clientAlgoId": "tp1"},
            {"side": "SELL", "type": "STOP_MARKET", "triggerPrice": "98.2", "clientAlgoId": "sl1"},
        ]
    )
    trader._recover_position_state_from_binance(
        state,
        {
            "symbol": "BTCUSDT",
            "positionAmt": "0.5",
            "entryPrice": "100.0",
            "markPrice": "101.0",
        },
        client,
    )

    pos = state["positions"]["BTCUSDT"]
    assert pos["direction"] == "LONG"
    assert pos["tp_price"] == 105.1
    assert pos["sl_price"] == 98.2
    assert pos["tp_order_id"] == "algo:tp1"
    assert pos["sl_order_id"] == "algo:sl1"
    assert pos["protection_status"] == "armed"


def test_recover_position_state_uses_mark_price_when_entry_missing():
    state = _fresh_state()
    client = _AlgoRecoveryClient(algo_orders=[])
    trader._recover_position_state_from_binance(
        state,
        {
            "symbol": "ETHUSDT",
            "positionAmt": "-2",
            "entryPrice": "0",
            "markPrice": "250.55",
        },
        client,
    )

    pos = state["positions"]["ETHUSDT"]
    assert pos["direction"] == "SHORT"
    assert pos["entry_price"] == 250.55
    assert pos["protection_status"] == "untracked"


def test_size_position_returns_none_for_invalid_inputs():
    assert trader.size_position(1000, 1.5, 0, 100, 3, 0.001, 5.0) is None
    assert trader.size_position(1000, 1.5, 2, 0, 3, 0.001, 5.0) is None


def test_size_position_rejects_below_min_qty():
    qty = trader.size_position(100, 1.5, 50, 1000, 3, 1.0, 5.0)
    assert qty is None


def test_size_position_rejects_below_min_notional():
    qty = trader.size_position(100, 1.5, 10, 1000, 3, 0.001, 50.0)
    assert qty is None


def test_size_position_success_caps_by_equity_per_position():
    qty = trader.size_position(1000, 1.5, 1.0, 100.0, 3, 0.001, 5.0, max_positions=2)
    assert qty == 5.0


def test_size_position_is_stable_across_sequential_calls_with_same_equity():
    # Regression: previously this used available `balance` which shrank as
    # margin got locked, producing smaller successive positions. Now sizing
    # is driven by total equity, so identical inputs must produce identical
    # sizes regardless of how many positions are already open.
    args = (1000.0, 1.5, 1.0, 100.0, 3, 0.001, 5.0)
    sizes = [trader.size_position(*args, max_positions=5) for _ in range(5)]
    assert all(s == sizes[0] for s in sizes), f"sizes drifted: {sizes}"


def test_required_threshold_by_engine_and_strategy():
    assert trader._required_threshold("rule_match", "anything") is None
    assert trader._required_threshold("combined", "rule_statistical") is None
    assert trader._required_threshold("combined", "breakout_long") == trader.SIGNAL_THRESHOLD_BREAKOUT
    assert trader._required_threshold("combined", "trend_pullback") == trader.SIGNAL_THRESHOLD_TREND


def test_cooldown_helpers_roundtrip():
    state = _fresh_state()
    assert trader._in_cooldown(state, "BTCUSDT", 24) is False
    trader._set_cooldown(state, "BTCUSDT")
    assert trader._in_cooldown(state, "BTCUSDT", 24) is True


def test_circuit_breaker_active_false_when_missing():
    assert trader._circuit_breaker_active({"circuit_breaker_until": None}) is False


def test_check_circuit_breaker_updates_peak_and_triggers(monkeypatch):
    sent = []
    monkeypatch.setattr(trader, "send_telegram", lambda msg: sent.append(msg))
    state = {"peak_equity": 1000.0, "circuit_breaker_until": None}
    cfg = {"max_drawdown_pct": 10.0, "circuit_break_hours": 1}

    trader._check_circuit_breaker(state, 1200.0, cfg)
    assert state["peak_equity"] == 1200.0

    trader._check_circuit_breaker(state, 1000.0, cfg)
    assert state["circuit_breaker_until"] is not None
    assert sent and "CIRCUIT BREAKER TRIGGERED" in sent[0]


def test_monitor_positions_records_tp_and_cancels_other_leg(monkeypatch):
    sent = []
    monkeypatch.setattr(trader, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(trader, "save_state", lambda state: None)
    state = _fresh_state()
    state["positions"]["BTCUSDT"] = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "entry_price": 100.0,
        "entry_time": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "quantity": 1.0,
        "position_size_usdt": 100.0,
        "tp_order_id": "tp1",
        "sl_order_id": "sl1",
        "score": 7.0,
        "strategy": "breakout",
        "signal_engine": "combined",
        "selected_source": "technical",
        "setup_name": "breakout",
        "market_regime": "bull",
        "protection_status": "armed",
    }
    client = _MonitorClient()
    client.statuses["tp1"] = "FILLED"
    client.fill_prices["tp1"] = 105.0

    trader.monitor_positions(state, client, {"timeout_hours": 180}, dry_run=False)

    assert "BTCUSDT" not in state["positions"]
    assert state["trade_log"][0]["exit_reason"] == "TP"
    assert client.cancelled == [("BTCUSDT", "sl1")]
    assert sent and "Protection: <code>armed</code>" in sent[0]


def test_monitor_positions_timeout_close_failure_keeps_position(monkeypatch):
    sent = []
    monkeypatch.setattr(trader, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(trader, "save_state", lambda state: None)
    state = _fresh_state()
    state["positions"]["ETHUSDT"] = {
        "symbol": "ETHUSDT",
        "direction": "SHORT",
        "entry_price": 200.0,
        "entry_time": (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat(),
        "quantity": 1.0,
        "position_size_usdt": 200.0,
        "tp_order_id": "tp1",
        "sl_order_id": "sl1",
        "score": 6.0,
        "strategy": "trend_pullback",
    }
    client = _MonitorClient()

    trader.monitor_positions(state, client, {"timeout_hours": 180}, dry_run=False)

    assert "ETHUSDT" in state["positions"]
    assert sent and "TIMEOUT close FAILED" in sent[0]
    assert client.closed == [("ETHUSDT", "BUY", 1.0)]


def test_execute_entry_rejects_missing_symbol_info(monkeypatch):
    monkeypatch.setattr(trader, "send_telegram", lambda msg: None)
    state = _fresh_state()
    client = _EntryClient(symbol_info=None)
    signal = _make_signal(strategy="breakout", entry_price=100.0, tp=104.0, sl=98.0, tp_pct=4.0, sl_pct=2.0, score=7.0)
    cfg = {"risk_pct": 1.5, "max_positions": 3, "signal_engine": "combined"}

    assert trader.execute_entry(signal, state, client, cfg, dry_run=False, equity=1000.0) is False


def test_execute_entry_handles_market_order_failure(monkeypatch):
    monkeypatch.setattr(trader, "send_telegram", lambda msg: None)
    state = _fresh_state()
    client = _EntryClient(market_fill=None)
    signal = _make_signal(strategy="breakout", entry_price=100.0, tp=104.0, sl=98.0, tp_pct=4.0, sl_pct=2.0, score=7.0)
    cfg = {"risk_pct": 1.5, "max_positions": 3, "signal_engine": "combined"}

    assert trader.execute_entry(signal, state, client, cfg, dry_run=False, equity=1000.0) is False


def test_execute_entry_handles_tp_failure_with_emergency_close(monkeypatch):
    sent = []
    monkeypatch.setattr(trader, "send_telegram", lambda msg: sent.append(msg))
    state = _fresh_state()
    client = _EntryClient(
        market_fill={"filled_price": 101.0},
        tp_order_id=None,
        market_close={"filled_price": 101.0, "status": "FILLED"},
    )
    signal = _make_signal(strategy="breakout", entry_price=100.0, tp=104.0, sl=98.0, tp_pct=4.0, sl_pct=2.0, score=7.0)
    cfg = {"risk_pct": 1.5, "max_positions": 3, "signal_engine": "combined"}

    assert trader.execute_entry(signal, state, client, cfg, dry_run=False, equity=1000.0) is False
    assert "BTCUSDT" not in state["positions"]
    assert state["last_signal"]["BTCUSDT"]
    assert client.close_calls


def test_execute_entry_handles_sl_failure_and_cancels_tp(monkeypatch):
    sent = []
    monkeypatch.setattr(trader, "send_telegram", lambda msg: sent.append(msg))
    state = _fresh_state()
    client = _EntryClient(
        market_fill={"filled_price": 101.0},
        tp_order_id="algo:tp1",
        sl_order_id=None,
        market_close={"filled_price": 101.0, "status": "FILLED"},
    )
    signal = _make_signal(
        symbol="BTCUSDT", direction="SHORT",
        entry_price=100.0, tp=96.0, sl=102.0,
        tp_pct=4.0, sl_pct=2.0, score=7.0, strategy="breakout",
    )
    cfg = {"risk_pct": 1.5, "max_positions": 3, "signal_engine": "combined"}

    assert trader.execute_entry(signal, state, client, cfg, dry_run=False, equity=1000.0) is False
    assert client.cancel_calls == [("BTCUSDT", "algo:tp1")]


def test_reconcile_with_binance_imports_unknown_position(monkeypatch):
    sent = []
    saves = []
    monkeypatch.setattr(trader, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(trader, "save_state", lambda state: saves.append(copy.deepcopy(state)))
    state = _fresh_state()

    class _Client:
        def get_open_positions(self):
            return [{
                "symbol": "BTCUSDT",
                "positionAmt": "1",
                "entryPrice": "100",
                "markPrice": "101",
            }]

        def get_symbol_info(self, symbol):
            return {"price_precision": 2}

        def get_open_algo_orders(self, symbol):
            return []

    trader.reconcile_with_binance(state, _Client())

    assert "BTCUSDT" in state["positions"]
    assert sent and "Recovered open Binance positions" in sent[0]
    assert saves


def test_pid_lock_acquire_and_release(tmp_path, monkeypatch):
    pid_file = tmp_path / "trader.pid"
    monkeypatch.setattr(trader, "_PID_FILE", str(pid_file))
    monkeypatch.setattr(trader.os, "getpid", lambda: 4242)

    assert trader._acquire_pid_lock() is True
    assert pid_file.read_text() == "4242"

    trader._release_pid_lock()
    assert not pid_file.exists()


def test_pid_lock_rejects_running_process(tmp_path, monkeypatch):
    pid_file = tmp_path / "trader.pid"
    pid_file.write_text("999")
    monkeypatch.setattr(trader, "_PID_FILE", str(pid_file))
    monkeypatch.setattr(trader.os, "kill", lambda pid, sig: None)

    assert trader._acquire_pid_lock() is False


def test_build_startup_banner_contains_version_and_engine(monkeypatch):
    monkeypatch.setattr(trader.os, "getpid", lambda: 1234)
    banner = trader._build_startup_banner(
        {"risk_pct": 1.5, "max_positions": 3, "cooldown_hours": 24},
        "combined",
        "[LIVE] ",
        "abc123",
    )
    assert any("Version:        abc123" in line for line in banner)
    assert any("Signal engine:  combined" in line for line in banner)
