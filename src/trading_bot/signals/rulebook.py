"""Rulebook loading, scope filtering, and rule matching.

A "rulebook" is a JSON file of validated setups (each: name, conditions,
template, direction, tp_sl, filter, train_stats, test_stats). load_rulebook
parses + caches it. _find_matching_rules returns rules that fire on the
current snapshot.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from ..core.indicators import Snapshot
from ..logging_setup import dbg, log
from ..setup_conditions import ALL_CONDITIONS, matches_conditions, normalize_conditions
from .config import RULEBOOK_PATH
from .scoring import suggest_tp_sl


_rulebook_cache = {"path": None, "mtime": None, "data": None}
_rulebook_missing_warned: set = set()


def load_rulebook(rulebook_path: Optional[str] = None) -> dict:
    """Load the validated rules export from disk, with mtime-based caching."""
    path = rulebook_path or RULEBOOK_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        if path not in _rulebook_missing_warned:
            dbg.debug(f"Rulebook not found: {path}")
            _rulebook_missing_warned.add(path)
        return {"long": [], "short": []}

    cache_hit = (
        _rulebook_cache["path"] == path
        and _rulebook_cache["mtime"] == mtime
        and _rulebook_cache["data"] is not None
    )
    if cache_hit:
        return _rulebook_cache["data"]

    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        log.warning(f"Could not load rulebook from {path}: {exc}")
        return {"long": [], "short": []}

    raw = payload.get("validated_setups", payload)
    normalized: dict[str, list] = {"long": [], "short": []}
    for bucket in ("long", "short"):
        for rule in raw.get(bucket, []):
            template = rule.get("template", rule.get("profile", "standard"))
            normalized_conditions = normalize_conditions(rule.get("conditions", []))
            unknown = [c for c in normalized_conditions if c not in ALL_CONDITIONS]
            if unknown:
                raise ValueError(
                    f"Rule '{rule.get('name', '')}' in {path} uses unknown condition(s): "
                    + ", ".join(unknown)
                )
            # Scope filter — stored as nested dict; flat fields kept for backward compat
            raw_filter = rule.get("filter") or {}
            normalized[bucket].append(
                {
                    "name": rule.get("name", ""),
                    "template": "wide" if template == "breakout" else ("standard" if template == "trend" else template),
                    "direction": rule.get("direction", bucket.upper()),
                    "conditions": normalized_conditions,
                    "tp_sl": dict(rule.get("tp_sl", {})),
                    "train_stats": dict(rule.get("train_stats", {})),
                    "test_stats": dict(rule.get("test_stats", {})),
                    "by_symbol": dict(rule.get("by_symbol", {})),
                    "filter": {
                        "symbol": raw_filter.get("symbol") or rule.get("scope_symbol"),
                        "regime": raw_filter.get("regime") or rule.get("scope_regime"),
                    },
                }
            )

    _rulebook_cache.update({"path": path, "mtime": mtime, "data": normalized})
    return normalized


# Keep old name as alias so external callers / tests don't break immediately
load_validated_setups = load_rulebook

def _rule_match_sort_key(rule: dict) -> tuple:
    """Rank matching rules: most specific scope first, then by out-of-sample profit factor."""
    test_stats = rule.get("test_stats", {})
    return (
        -_rule_specificity(rule),
        -float(test_stats.get("profit_factor", 0.0) or 0.0),
        -float(test_stats.get("avg_pnl_pct", 0.0) or 0.0),
        -float(test_stats.get("edge_win_rate", 0.0) or 0.0),
        -int(test_stats.get("count", 0) or 0),
        len(rule.get("conditions", [])),
        rule.get("name", ""),
    )


def _rule_match_score(rule: dict) -> float:
    """Score a matched rule by its out-of-sample profit factor."""
    return round(float(rule.get("test_stats", {}).get("profit_factor", 0.0) or 0.0), 4)


def _resolve_rulebook_path(signal_engine: str, rulebook_path: Optional[str]) -> str:
    """Return the rulebook file path for the requested engine."""
    if rulebook_path:
        return rulebook_path
    return RULEBOOK_PATH


def _rule_specificity(rule: dict) -> int:
    """Rank rule specificity: symbol-scoped > regime-scoped > pooled."""
    f = rule.get("filter", {})
    if f.get("symbol"):
        return 2
    if f.get("regime"):
        return 1
    return 0


def _rule_matches_context(rule: dict, symbol: str, current_regime: Optional[str] = None) -> bool:
    """Return True if this rule applies to the current symbol and market regime.

    Regime-scoped rules FAIL CLOSED: when the regime is unknown (None or
    "unknown" — classification failed, warmup, or caller didn't classify),
    a rule that requires a specific regime does NOT fire. This matches the
    analyzer, which excludes unknown-regime rows from mining, so scoped rules
    only ever trade in conditions they were validated on. Pooled and
    symbol-scoped rules are unaffected.
    """
    f = rule.get("filter", {})
    if f.get("symbol") and f["symbol"] != symbol:
        return False
    if f.get("regime"):
        if current_regime is None or current_regime == "unknown":
            return False  # fail closed: regime required but not known
        if f["regime"] != current_regime:
            return False
    return True


# Backward-compat aliases used by tests
_setup_scope_specificity = _rule_specificity
_setup_matches_scope = _rule_matches_context

def _find_matching_rules(
    symbol: str,
    snapshot,
    rulebook: dict,
    current_regime: Optional[str] = None,
) -> list[dict]:
    """Return rules from the rulebook whose conditions match the current candle snapshot."""
    snap_dict = snapshot.to_dict() if isinstance(snapshot, Snapshot) else (snapshot or {})
    matches = []

    for bucket, direction in (("long", "LONG"), ("short", "SHORT")):
        for rule in rulebook.get(bucket, []):
            if rule.get("direction") != direction:
                continue
            if not _rule_matches_context(rule, symbol, current_regime):
                continue
            if not matches_conditions(snap_dict, rule.get("conditions", [])):
                continue
            matches.append(dict(rule))

    matches.sort(key=_rule_match_sort_key)
    return matches


# Backward-compat alias
_find_matching_statistical_setups = _find_matching_rules

def _suggest_tp_sl_for_setup(candles_1h: list, direction: str, matched_setup: dict) -> dict:
    """Use the validated setup's stored ATR/RR template directly."""
    tp_sl = matched_setup.get("tp_sl", {})
    return suggest_tp_sl(
        candles_1h,
        direction,
        multiplier_sl=float(tp_sl.get("sl_atr_mult", 1.5) or 1.5),
        rr_ratio=float(tp_sl.get("rr_ratio", 2.0) or 2.0),
    )


def _suggest_tp_sl_for_strategy(candles_1h: list, direction: str, strategy: str) -> dict:
    """Return ATR TP/SL parameters for legacy technical strategies."""
    if strategy == "mean_reversion":
        return suggest_tp_sl(candles_1h, direction, multiplier_sl=1.0, rr_ratio=1.5)
    if strategy == "breakout":
        return suggest_tp_sl(candles_1h, direction, multiplier_sl=2.0, rr_ratio=2.5)
    return suggest_tp_sl(candles_1h, direction, multiplier_sl=1.5, rr_ratio=2.0)
