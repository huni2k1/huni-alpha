import json
from datetime import datetime, timezone

import pytest

from trading_bot import binance_http as _bhttp
from trading_bot import scanner
from trading_bot.signals import engine as _sig_engine
from trading_bot.signals import rulebook as _sig_rulebook


class _Response:
    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def json(self):
        return self._payload


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
    monkeypatch.setattr(_bhttp.candle_cache, "load_from_cache", lambda *args, **kwargs: [[1], [2], [3], [4]])
    monkeypatch.setattr(_bhttp, "fetch_klines", lambda *args, **kwargs: pytest.fail("should not hit API"))

    result = scanner.fetch_klines_cached("BTCUSDT", "1h", 2, use_cache=True)

    assert result == [[3], [4]]


def test_fetch_klines_cached_cache_miss_fetches_and_saves(monkeypatch):
    saved = []
    monkeypatch.setattr(_bhttp.candle_cache, "load_from_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(_bhttp, "fetch_klines", lambda *args, **kwargs: [[10], [11]])
    monkeypatch.setattr(_bhttp.candle_cache, "save_to_cache", lambda *args, **kwargs: saved.append(args) or True)

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
    monkeypatch.setattr(_sig_rulebook, "_rulebook_cache", {"path": None, "mtime": None, "data": None})
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
    monkeypatch.setattr(_sig_rulebook, "_rulebook_cache", {"path": str(rulebook_path), "mtime": mtime, "data": cached})

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
    monkeypatch.setattr(_sig_engine, "_build_indicator_snapshot", lambda *args, **kwargs: {"rsi": 40.0})
    monkeypatch.setattr(_sig_engine, "load_rulebook", lambda path: {"long": [], "short": []})
    monkeypatch.setattr(_sig_engine, "_find_matching_rules", lambda *args, **kwargs: [])
    scanner._last_rejection_reason.clear()

    result = scanner._generate_rule_match_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60, "rule_match")

    assert result is None
    assert scanner._last_rejection_reason["BTCUSDT"] == "No matching rule"


def test_generate_rule_match_signal_rejects_whipsaw(monkeypatch):
    monkeypatch.setattr(_sig_engine, "_build_indicator_snapshot", lambda *args, **kwargs: {"rsi": 20.0})
    monkeypatch.setattr(_sig_engine, "load_rulebook", lambda path: {"long": [], "short": []})
    monkeypatch.setattr(_sig_engine, "_find_matching_rules", lambda *args, **kwargs: [{"name": "rule1", "direction": "LONG", "template": "standard", "conditions": ["rsi_below_28"], "tp_sl": {}, "filter": {}, "test_stats": {}, "train_stats": {}}])
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
    monkeypatch.setattr(_sig_engine, "_build_indicator_snapshot", lambda *args, **kwargs: {"rsi": 20.0})
    monkeypatch.setattr(_sig_engine, "load_rulebook", lambda path: {"long": [], "short": []})
    monkeypatch.setattr(
        _sig_engine,
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
    monkeypatch.setattr(_sig_engine, "is_whipsaw", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        _sig_engine,
        "_suggest_tp_sl_for_setup",
        lambda candles, direction, matched_setup: {
            "entry_price": 100.0,
            "suggested_tp": 95.0,
            "suggested_sl": 102.0,
            "tp_pct": 5.0,
            "sl_pct": 2.0,
            "atr": 1.0,
            "sl_atr_mult": 2.0,
            "rr_ratio": 2.5,
        },
    )

    result = scanner._generate_rule_match_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60, "rule_match")

    assert result["direction"] == "SHORT"
    assert result["strategy"] == "rule_wide"
    assert result["statistical_setup"] == "rule1"
    assert result["statistical_details"]["candidate_count"] == 1


def test_generate_ta_score_signal_returns_none_for_neutral(monkeypatch):
    monkeypatch.setattr(_sig_engine, "score_technical", lambda *args, **kwargs: {"direction": "NEUTRAL"})
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
        _sig_engine,
        "score_technical",
        lambda *args, **kwargs: {"direction": "LONG", "long_score": 2.0, "short_score": 1.4, "details": {}},
    )
    scanner._last_rejection_reason.clear()

    result = scanner._generate_ta_score_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60)

    assert result is None
    assert "Ambiguous" in scanner._last_rejection_reason["BTCUSDT"]


def test_generate_ta_score_signal_rejects_whipsaw(monkeypatch):
    monkeypatch.setattr(
        _sig_engine,
        "score_technical",
        lambda *args, **kwargs: {
            "direction": "LONG",
            "long_score": 3.0,
            "short_score": 0.5,
            "details": {"strategy": "trend_pullback", "regime": "trending"},
        },
    )
    monkeypatch.setattr(_sig_engine, "is_whipsaw", lambda *args, **kwargs: True)
    scanner._last_rejection_reason.clear()

    result = scanner._generate_ta_score_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60, state={"signal_history": {}})

    assert result is None
    assert scanner._last_rejection_reason["BTCUSDT"] == "Whipsaw"


def test_generate_combined_signal_includes_selected_reason(monkeypatch):
    monkeypatch.setattr(_sig_engine, "_generate_rule_match_signal", lambda *args, **kwargs: {
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
    monkeypatch.setattr(_sig_engine, "_generate_ta_score_signal", lambda *args, **kwargs: None)
    scanner._last_rejection_reason.clear()
    scanner._last_rejection_reason["BTCUSDT"] = "No matching rule"

    result = scanner._generate_combined_signal("BTCUSDT", [[1, 1, 1, 1, 1]] * 60)

    assert result["hybrid_details"]["selected"] == {
        "source": "statistical",
        "reason": "rule matched",
    }


def test_generate_combined_signal_sets_technical_fallback_reason(monkeypatch):
    monkeypatch.setattr(_sig_engine, "_generate_rule_match_signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(_sig_engine, "_generate_ta_score_signal", lambda *args, **kwargs: {
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

