import json
from datetime import datetime, timezone

import pytest

from trading_bot import scanner


class _Response:
    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def json(self):
        return self._payload


def test_load_state_returns_default_for_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "STATE_FILE", str(tmp_path / "missing.json"))

    assert scanner.load_state() == {"alerts": {}}


def test_load_state_normalizes_missing_or_bad_alerts(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"alerts": ["bad"], "signal_history": "oops"}))
    monkeypatch.setattr(scanner, "STATE_FILE", str(state_path))

    state = scanner.load_state()

    assert state["alerts"] == {}
    assert state["signal_history"] == {}


def test_can_alert_and_mark_alert_work_with_empty_state(monkeypatch):
    state = {}
    monkeypatch.setattr(scanner.time, "time", lambda: 10000.0)

    assert scanner.can_alert(state, "BTCUSDT", "LONG", "ENTRY") is True
    scanner.mark_alert(state, "BTCUSDT", "LONG", "ENTRY")
    assert scanner.can_alert(state, "BTCUSDT", "LONG", "ENTRY") is False


def test_is_asia_session_boundaries():
    assert scanner._is_asia_session(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)) is True
    assert scanner._is_asia_session(datetime(2026, 1, 1, 7, 59, tzinfo=timezone.utc)) is True
    assert scanner._is_asia_session(datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)) is False


def test_get_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Response(error=RuntimeError("temporary"))
        return _Response(payload={"ok": True})

    monkeypatch.setattr(scanner.SESSION, "get", fake_get)
    monkeypatch.setattr(scanner.time, "sleep", lambda _: None)

    assert scanner.get("https://example.com") == {"ok": True}
    assert calls["n"] == 2


def test_get_returns_none_after_retries(monkeypatch):
    monkeypatch.setattr(scanner.SESSION, "get", lambda *args, **kwargs: _Response(error=RuntimeError("boom")))
    monkeypatch.setattr(scanner.time, "sleep", lambda _: None)

    assert scanner.get("https://example.com") is None


def test_fetch_klines_cached_returns_trimmed_cache_hit(monkeypatch):
    monkeypatch.setattr(scanner.candle_cache, "load_from_cache", lambda *args, **kwargs: [[1], [2], [3], [4]])
    monkeypatch.setattr(scanner, "fetch_klines", lambda *args, **kwargs: pytest.fail("should not hit API"))

    result = scanner.fetch_klines_cached("BTCUSDT", "1h", 2, use_cache=True)

    assert result == [[3], [4]]


def test_fetch_klines_cached_cache_miss_fetches_and_saves(monkeypatch):
    saved = []
    monkeypatch.setattr(scanner.candle_cache, "load_from_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner, "fetch_klines", lambda *args, **kwargs: [[10], [11]])
    monkeypatch.setattr(scanner.candle_cache, "save_to_cache", lambda *args, **kwargs: saved.append(args) or True)

    result = scanner.fetch_klines_cached("BTCUSDT", "1h", 2, use_cache=True)

    assert result == [[10], [11]]
    assert saved


def test_load_rulebook_normalizes_rule_fields(tmp_path):
    rulebook_path = tmp_path / "rules.json"
    rulebook_path.write_text(json.dumps({
        "validated_setups": {
            "long": [{
                "name": "trend_long",
                "template": "trend",
                "direction": "LONG",
                "conditions": ["rsi_below_28"],
                "tp_sl": {"sl_atr_mult": 1.2, "rr_ratio": 2.1},
                "filter": {"symbol": "BTCUSDT", "regime": "bull"},
                "test_stats": {"profit_factor": 1.5},
            }],
            "short": [{
                "name": "breakout_short",
                "template": "breakout",
                "direction": "SHORT",
                "conditions": ["rsi_below_30"],
            }],
        }
    }))

    rules = scanner.load_rulebook(str(rulebook_path))

    assert rules["long"][0]["template"] == "standard"
    assert rules["long"][0]["filter"] == {"symbol": "BTCUSDT", "regime": "bull"}
    assert rules["short"][0]["template"] == "wide"


def test_load_rulebook_rejects_unknown_conditions(tmp_path):
    rulebook_path = tmp_path / "rules.json"
    rulebook_path.write_text(json.dumps({
        "long": [{"name": "bad", "direction": "LONG", "conditions": ["not_a_real_condition"]}],
        "short": [],
    }))

    with pytest.raises(ValueError):
        scanner.load_rulebook(str(rulebook_path))


def test_load_rulebook_returns_empty_on_read_error(monkeypatch):
    monkeypatch.setattr(scanner.os.path, "exists", lambda path: True)
    monkeypatch.setattr(scanner.os.path, "getmtime", lambda path: 1.0)
    monkeypatch.setattr(scanner, "_rulebook_cache", {"path": None, "mtime": None, "data": None})
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no file")))

    rules = scanner.load_rulebook("/tmp/missing.json")

    assert rules == {"long": [], "short": []}


def test_load_rulebook_returns_empty_when_file_missing(monkeypatch):
    missing = set()
    monkeypatch.setattr(scanner.os.path, "getmtime", lambda path: (_ for _ in ()).throw(OSError("missing")))
    monkeypatch.setattr(scanner.dbg, "debug", lambda msg: missing.add(msg))
    scanner._rulebook_missing_warned.clear()

    rules = scanner.load_rulebook("/tmp/does-not-exist.json")

    assert rules == {"long": [], "short": []}
    assert any("Rulebook not found" in msg for msg in missing)


def test_load_rulebook_uses_cache_hit(tmp_path, monkeypatch):
    rulebook_path = tmp_path / "rules.json"
    rulebook_path.write_text(json.dumps({"long": [], "short": []}))
    mtime = rulebook_path.stat().st_mtime
    cached = {"long": [{"name": "cached"}], "short": []}
    monkeypatch.setattr(scanner, "_rulebook_cache", {"path": str(rulebook_path), "mtime": mtime, "data": cached})

    rules = scanner.load_rulebook(str(rulebook_path))

    assert rules is cached


def test_rule_match_helper_functions():
    pooled = {"filter": {}, "conditions": ["rsi_below_30"], "name": "a", "test_stats": {"profit_factor": 1.2}}
    symbol = {"filter": {"symbol": "BTCUSDT"}, "conditions": ["rsi_below_30"], "name": "b", "test_stats": {"profit_factor": 1.4, "avg_pnl_pct": 0.5, "edge_win_rate": 10, "count": 3}}

    assert scanner._rule_specificity(pooled) == 0
    assert scanner._rule_specificity(symbol) == 2
    assert scanner._rule_match_score(symbol) == 1.4
    assert scanner._resolve_rulebook_path("combined", None) == scanner.RULEBOOK_PATH
    assert scanner._resolve_rulebook_path("combined", "/tmp/x.json") == "/tmp/x.json"
    assert scanner._rule_match_sort_key(symbol) < scanner._rule_match_sort_key(pooled)


def test_rule_matches_context_respects_symbol_and_regime():
    rule = {"filter": {"symbol": "BTCUSDT", "regime": "bull"}}
    assert scanner._rule_matches_context(rule, "BTCUSDT", "bull") is True
    assert scanner._rule_matches_context(rule, "ETHUSDT", "bull") is False
    assert scanner._rule_matches_context(rule, "BTCUSDT", "bear") is False
    assert scanner._rule_matches_context(rule, "BTCUSDT", None) is True


def test_generate_rule_match_signal_records_no_match_reason(monkeypatch):
    monkeypatch.setattr(scanner, "_build_indicator_snapshot", lambda *args, **kwargs: {"rsi": 40.0})
    monkeypatch.setattr(scanner, "load_rulebook", lambda path: {"long": [], "short": []})
    monkeypatch.setattr(scanner, "_find_matching_rules", lambda *args, **kwargs: [])
    scanner._last_rejection_reason.clear()

    result = scanner._generate_rule_match_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60, "rule_match")

    assert result is None
    assert scanner._last_rejection_reason["BTCUSDT"] == "No matching rule"


def test_generate_rule_match_signal_rejects_whipsaw(monkeypatch):
    monkeypatch.setattr(scanner, "_build_indicator_snapshot", lambda *args, **kwargs: {"rsi": 20.0})
    monkeypatch.setattr(scanner, "load_rulebook", lambda path: {"long": [], "short": []})
    monkeypatch.setattr(scanner, "_find_matching_rules", lambda *args, **kwargs: [{"name": "rule1", "direction": "LONG", "template": "standard", "conditions": ["rsi_below_28"], "tp_sl": {}, "filter": {}, "test_stats": {}, "train_stats": {}}])
    monkeypatch.setattr(scanner.time, "time", lambda: 1050.0)
    scanner._last_rejection_reason.clear()

    result = scanner._generate_rule_match_signal(
        "BTCUSDT",
        [[1, 1, 1, 1, 1]] * 60,
        "rule_match",
        state={"signal_history": {"BTCUSDT": {"direction": "SHORT", "ts": 1000.0}}},
    )

    assert result is None
    assert scanner._last_rejection_reason["BTCUSDT"] == "Whipsaw"


def test_generate_rule_match_signal_builds_full_signal(monkeypatch):
    monkeypatch.setattr(scanner, "_build_indicator_snapshot", lambda *args, **kwargs: {"rsi": 20.0})
    monkeypatch.setattr(scanner, "load_rulebook", lambda path: {"long": [], "short": []})
    monkeypatch.setattr(
        scanner,
        "_find_matching_rules",
        lambda *args, **kwargs: [{
            "name": "rule1",
            "direction": "SHORT",
            "template": "wide",
            "conditions": ["rsi_below_28"],
            "tp_sl": {"sl_atr_mult": 2.0, "rr_ratio": 2.5},
            "filter": {"regime": "bull"},
            "test_stats": {"profit_factor": 1.8},
            "train_stats": {"profit_factor": 1.6},
        }],
    )
    monkeypatch.setattr(scanner, "is_whipsaw", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        scanner,
        "_suggest_tp_sl_for_setup",
        lambda candles, direction, matched_setup: {
            "entry_price": 100.0,
            "suggested_tp": 95.0,
            "suggested_sl": 102.0,
            "tp_pct": 5.0,
            "sl_pct": 2.0,
            "atr": 1.0,
            "rr_ratio": 2.5,
        },
    )

    result = scanner._generate_rule_match_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60, "rule_match")

    assert result["direction"] == "SHORT"
    assert result["strategy"] == "rule_wide"
    assert result["statistical_setup"] == "rule1"
    assert result["statistical_details"]["candidate_count"] == 1


def test_generate_ta_score_signal_returns_none_for_neutral(monkeypatch):
    monkeypatch.setattr(scanner, "score_technical", lambda *args, **kwargs: {"direction": "NEUTRAL"})
    assert scanner._generate_ta_score_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60) is None


def test_generate_ta_score_signal_marks_no_signal_when_totals_weak(monkeypatch):
    monkeypatch.setattr(
        scanner,
        "score_technical",
        lambda *args, **kwargs: {"direction": "LONG", "long_score": 0.2, "short_score": 0.1, "details": {}},
    )
    scanner._last_rejection_reason.clear()

    result = scanner._generate_ta_score_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60)

    assert result is None
    assert scanner._last_rejection_reason["BTCUSDT"] == "No Signal"


def test_generate_ta_score_signal_marks_ambiguous_gap(monkeypatch):
    monkeypatch.setattr(
        scanner,
        "score_technical",
        lambda *args, **kwargs: {"direction": "LONG", "long_score": 2.0, "short_score": 1.4, "details": {}},
    )
    scanner._last_rejection_reason.clear()

    result = scanner._generate_ta_score_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60)

    assert result is None
    assert "Ambiguous" in scanner._last_rejection_reason["BTCUSDT"]


def test_generate_ta_score_signal_rejects_whipsaw(monkeypatch):
    monkeypatch.setattr(
        scanner,
        "score_technical",
        lambda *args, **kwargs: {
            "direction": "LONG",
            "long_score": 3.0,
            "short_score": 0.5,
            "details": {"strategy": "trend_pullback", "regime": "trending"},
        },
    )
    monkeypatch.setattr(scanner, "is_whipsaw", lambda *args, **kwargs: True)
    scanner._last_rejection_reason.clear()

    result = scanner._generate_ta_score_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60, state={"signal_history": {}})

    assert result is None
    assert scanner._last_rejection_reason["BTCUSDT"] == "Whipsaw"


def test_generate_combined_signal_includes_selected_reason(monkeypatch):
    monkeypatch.setattr(scanner, "_generate_rule_match_signal", lambda *args, **kwargs: {
        "symbol": "BTCUSDT",
        "direction": "SHORT",
        "score": 1.4,
        "entry_price": 100.0,
        "tp": 96.0,
        "sl": 102.0,
        "tp_pct": 4.0,
        "sl_pct": 2.0,
        "atr": 1.0,
        "rr_ratio": 2.0,
        "technical_score": 0.0,
        "long_score": 0.0,
        "short_score": 1.4,
        "regime": "rule_match",
        "strategy": "rule_standard",
        "details": {"regime": "rule_match"},
        "signal_engine": "rule_match",
        "statistical_setup": "wide_short_rsi_below_28",
        "statistical_details": {"conditions": ["rsi_below_28"], "template": "wide"},
    })
    monkeypatch.setattr(scanner, "_generate_ta_score_signal", lambda *args, **kwargs: None)
    scanner._last_rejection_reason.clear()
    scanner._last_rejection_reason["BTCUSDT"] = "No matching rule"

    result = scanner._generate_combined_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60)

    assert result["hybrid_details"]["selected"] == {
        "source": "statistical",
        "reason": "rule matched",
    }


def test_generate_combined_signal_sets_technical_fallback_reason(monkeypatch):
    monkeypatch.setattr(scanner, "_generate_rule_match_signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner, "_generate_ta_score_signal", lambda *args, **kwargs: {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "score": 7.2,
        "entry_price": 100.0,
        "tp": 104.0,
        "sl": 98.0,
        "tp_pct": 4.0,
        "sl_pct": 2.0,
        "atr": 1.0,
        "rr_ratio": 2.0,
        "technical_score": 7.2,
        "long_score": 7.2,
        "short_score": 0.0,
        "regime": "trending",
        "strategy": "trend_pullback",
        "details": {"regime": "trending", "rsi": {"1h": 45}},
        "signal_engine": "ta_score",
    })
    scanner._last_rejection_reason.clear()

    result = scanner._generate_combined_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60)

    assert result["hybrid_details"]["selected"]["source"] == "technical"
    assert result["hybrid_details"]["selected"]["reason"] == "technical fallback (no matching rule)"


def test_format_alert_includes_strategy_and_rr():
    msg = scanner.format_alert({
        "direction": "LONG",
        "symbol": "BTCUSDT",
        "entry_price": 100.0,
        "score": 7.5,
        "technical_score": 6.0,
        "details": {"rsi": {"1h": 45.0}, "vol_ratio": 1.2, "adx": 27.5},
        "rr_ratio": 2.0,
        "tp": 104.0,
        "sl": 98.0,
        "tp_pct": 4.0,
        "sl_pct": 2.0,
        "atr": 1.23,
        "strategy": "trend_pullback",
        "regime": "trending",
    })

    assert "Trend Pullback" in msg
    assert "Trending" in msg
    assert "2.0:1 R:R" in msg


def test_signal_engine_banner_labels():
    assert scanner._signal_engine_banner("ta_score") == "TA scoring (RSI/EMA/MACD/ADX)"
    assert scanner._signal_engine_banner("rule_match") == "Rulebook pattern matching"
    assert scanner._signal_engine_banner("combined") == "Combined: rulebook + TA score fallback"
    assert scanner._signal_engine_banner("other") == "other"


def test_scan_symbol_skips_when_no_data(monkeypatch):
    warnings = []
    monkeypatch.setattr(scanner, "fetch_klines_cached", lambda *args, **kwargs: [])
    monkeypatch.setattr(scanner.log, "warning", lambda msg: warnings.append(msg))
    scanner._cycle_results.clear()

    scanner.scan_symbol("BTCUSDT", {})

    assert warnings
    assert "No 1h data" in warnings[0]
    assert scanner._cycle_results == []


def test_scan_symbol_skips_when_not_enough_completed_candles(monkeypatch):
    warnings = []
    candles = [[1.0, 2.0, 0.5, 1.5, 1000.0]] * 500
    monkeypatch.setattr(scanner, "fetch_klines_cached", lambda *args, **kwargs: candles)
    monkeypatch.setattr(scanner.log, "warning", lambda msg: warnings.append(msg))
    scanner._cycle_results.clear()

    scanner.scan_symbol("BTCUSDT", {})

    assert warnings
    assert "Not enough completed candles" in warnings[0]
    assert scanner._cycle_results == []


def test_scan_symbol_records_rejected_cycle_row(monkeypatch):
    info_logs = []
    candles = [[1.0, 2.0, 0.5, 100.0, 1000.0]] * 4001
    monkeypatch.setattr(scanner, "fetch_klines_cached", lambda *args, **kwargs: candles)
    monkeypatch.setattr(scanner, "generate_signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner.log, "info", lambda msg: info_logs.append(msg))
    scanner._cycle_results.clear()
    scanner._last_rejection_reason.clear()
    scanner._last_rejection_reason["BTCUSDT"] = "No matching rule"

    scanner.scan_symbol("BTCUSDT", {})

    assert any("No signal (No matching rule)" in line for line in info_logs)
    assert scanner._cycle_results[-1]["selected_source"] == "rejected"
    assert scanner._cycle_results[-1]["filter_reason"] == "No matching rule"


def test_scan_symbol_sends_entry_alert_for_rule_match_signal(monkeypatch):
    candles = [[1.0, 2.0, 0.5, 100.0, 1000.0]] * 4001
    sent = []
    saved = []
    marked = []
    info_logs = []

    monkeypatch.setattr(scanner, "fetch_klines_cached", lambda *args, **kwargs: candles)
    monkeypatch.setattr(scanner, "generate_signal", lambda *args, **kwargs: {
        "symbol": "BTCUSDT",
        "direction": "SHORT",
        "score": 0.0,
        "entry_price": 100.0,
        "tp": 96.0,
        "sl": 102.0,
        "tp_pct": 4.0,
        "sl_pct": 2.0,
        "atr": 1.0,
        "rr_ratio": 2.0,
        "technical_score": 0.0,
        "long_score": 0.0,
        "short_score": 0.0,
        "regime": "rule_match",
        "strategy": "rule_standard",
        "details": {"regime": "rule_match", "rsi": {"1h": 35.0}},
        "signal_engine": "rule_match",
    })
    monkeypatch.setattr(scanner, "can_alert", lambda *args, **kwargs: True)
    monkeypatch.setattr(scanner, "format_alert", lambda signal: f"alert:{signal['symbol']}")
    monkeypatch.setattr(scanner, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(scanner, "mark_alert", lambda state, symbol, direction, tier="ENTRY": marked.append((symbol, direction, tier)))
    monkeypatch.setattr(scanner, "save_state", lambda state: saved.append(True))
    monkeypatch.setattr(scanner.log, "info", lambda msg: info_logs.append(msg))
    scanner._cycle_results.clear()

    scanner.scan_symbol("BTCUSDT", {})

    assert sent == ["alert:BTCUSDT"]
    assert marked == [("BTCUSDT", "SHORT", "ENTRY")]
    assert saved == [True]
    assert scanner._cycle_results[-1]["total"] == scanner.ALERT_THRESHOLD_OPTB
    assert any("ENTRY sent for BTCUSDT SHORT" in line for line in info_logs)


def test_scan_symbol_emits_watch_alert(monkeypatch):
    candles = [[1.0, 2.0, 0.5, 100.0, 1000.0]] * 4001
    sent = []
    marked = []
    saved = []
    info_logs = []

    monkeypatch.setattr(scanner, "fetch_klines_cached", lambda *args, **kwargs: candles)
    monkeypatch.setattr(scanner, "generate_signal", lambda *args, **kwargs: {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "score": scanner.ALERT_THRESHOLD_SOFT + 0.2,
        "entry_price": 100.0,
        "tp": 103.0,
        "sl": 98.0,
        "tp_pct": 3.0,
        "sl_pct": 2.0,
        "atr": 1.0,
        "rr_ratio": 1.5,
        "technical_score": 4.1,
        "long_score": 4.1,
        "short_score": 0.0,
        "regime": "weak_trend",
        "strategy": "trend_pullback_weak",
        "details": {"regime": "weak_trend", "rsi": {"1h": 48.0}},
        "signal_engine": "ta_score",
    })
    monkeypatch.setattr(scanner, "can_alert", lambda *args, **kwargs: True)
    monkeypatch.setattr(scanner, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(scanner, "mark_alert", lambda state, symbol, direction, tier="WATCH": marked.append((symbol, direction, tier)))
    monkeypatch.setattr(scanner, "save_state", lambda state: saved.append(True))
    monkeypatch.setattr(scanner.log, "info", lambda msg: info_logs.append(msg))
    scanner._cycle_results.clear()

    scanner.scan_symbol("BTCUSDT", {})

    assert sent
    assert "WATCH" in sent[0]
    assert marked == [("BTCUSDT", "LONG", "WATCH")]
    assert saved == [True]
    assert any("WATCH sent for BTCUSDT LONG" in line for line in info_logs)


def test_scan_symbol_emits_high_conf_alert(monkeypatch):
    candles = [[1.0, 2.0, 0.5, 100.0, 1000.0]] * 4001
    sent = []
    marked = []
    saved = []
    info_logs = []

    monkeypatch.setattr(scanner, "fetch_klines_cached", lambda *args, **kwargs: candles)
    monkeypatch.setattr(scanner, "generate_signal", lambda *args, **kwargs: {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "score": scanner.ALERT_THRESHOLD_HARD + 0.5,
        "entry_price": 100.0,
        "tp": 104.0,
        "sl": 98.0,
        "tp_pct": 4.0,
        "sl_pct": 2.0,
        "atr": 1.0,
        "rr_ratio": 2.0,
        "technical_score": 8.1,
        "long_score": 8.1,
        "short_score": 0.0,
        "regime": "trending",
        "strategy": "trend_pullback",
        "details": {"regime": "trending", "rsi": {"1h": 61.0}},
        "signal_engine": "ta_score",
        "hybrid_details": {"selected": {"source": "technical"}},
    })
    monkeypatch.setattr(scanner, "can_alert", lambda *args, **kwargs: True)
    monkeypatch.setattr(scanner, "format_alert", lambda signal: "high-conf")
    monkeypatch.setattr(scanner, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(scanner, "mark_alert", lambda state, symbol, direction, tier="HIGH": marked.append((symbol, direction, tier)))
    monkeypatch.setattr(scanner, "save_state", lambda state: saved.append(True))
    monkeypatch.setattr(scanner.log, "info", lambda msg: info_logs.append(msg))
    scanner._cycle_results.clear()

    scanner.scan_symbol("BTCUSDT", {})

    assert sent == ["high-conf"]
    assert marked == [("BTCUSDT", "LONG", "HIGH")]
    assert saved == [True]
    assert any("HIGH CONF sent for BTCUSDT LONG" in line for line in info_logs)


def test_scanner_main_handles_scan_errors_and_empty_cycle(monkeypatch):
    info_logs = []
    error_logs = []
    sleep_calls = {"count": 0}

    monkeypatch.setattr(scanner, "DEFAULT_SIGNAL_ENGINE", "combined")
    monkeypatch.setattr(scanner, "SYMBOLS", ["BTCUSDT"])
    monkeypatch.setattr(scanner, "SCAN_INTERVAL", 300)
    monkeypatch.setattr(scanner, "load_state", lambda: {})
    monkeypatch.setattr(scanner, "send_telegram", lambda msg: None)
    monkeypatch.setattr(scanner.log, "info", lambda msg: info_logs.append(msg))
    monkeypatch.setattr(scanner.log, "error", lambda msg, *args, **kwargs: error_logs.append(msg))
    monkeypatch.setattr(scanner, "scan_symbol", lambda symbol, state: (_ for _ in ()).throw(RuntimeError("boom")))

    def _fake_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] >= 2:
            raise SystemExit(0)

    monkeypatch.setattr(scanner.time, "sleep", _fake_sleep)
    scanner._cycle_results.clear()
    scanner._last_rejection_reason.clear()

    with pytest.raises(SystemExit):
        scanner.main()

    assert any("Error scanning BTCUSDT: boom" in line for line in error_logs)
    assert any("(No signals computed this cycle)" in line for line in info_logs)
