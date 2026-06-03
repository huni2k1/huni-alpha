"""Signal generation engines.

Three engines:
  - ta_score    — pure technical scoring (RSI/EMA/MACD/ADX). Score must clear threshold.
  - rule_match  — match the candle snapshot against a rulebook of validated setups.
  - combined    — try rule_match first, fall back to ta_score.

generate_signal() is the public entry. It picks one of the three engines
based on the signal_engine arg (or DEFAULT_SIGNAL_ENGINE).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..core.types import MarketRegime, Signal, SignalEngine
from ..logging_setup import dbg
from .config import (
    DEFAULT_SIGNAL_ENGINE,
    VALID_SIGNAL_ENGINES,
    _ENGINE_COMPAT_ALIASES,
)
from .filters import _is_asia_session, _last_rejection_reason, is_whipsaw
from .rulebook import (
    _find_matching_rules,
    _resolve_rulebook_path,
    _rule_match_score,
    _suggest_tp_sl_for_setup,
    _suggest_tp_sl_for_strategy,
    load_rulebook,
)
from .scoring import score_technical
from .snapshot import _build_indicator_snapshot


def _emit_signal(payload: dict) -> dict:
    """Validate and normalize a signal via the typed Signal model.

    Round-trips through Signal.from_dict -> to_dict so consumers keep
    receiving a dict. The Signal construction step catches missing fields
    and wrong types at the scanner boundary instead of failing downstream.

    To migrate consumers to typed Signal: drop `.to_dict()` here and update
    callers one at a time to read `signal.x` instead of `signal["x"]`.
    """
    return Signal.from_dict(payload).to_dict()

def _generate_rule_match_signal(
    symbol: str,
    candles_1h: list,
    signal_engine: str,
    state: dict = None,
    current_time: Optional[datetime] = None,
    precomputed_indicators: dict = None,
    rulebook_path: Optional[str] = None,
    current_regime: Optional[str] = None,
) -> Optional[dict]:
    """Match the current candle snapshot against the rulebook; return signal if a rule fires."""
    snapshot = _build_indicator_snapshot(
        symbol,
        candles_1h,
        current_time=current_time,
        precomputed_indicators=precomputed_indicators,
    )
    if snapshot is None:
        return None
    resolved_path = _resolve_rulebook_path(signal_engine, rulebook_path)
    rulebook = load_rulebook(resolved_path)
    matches = _find_matching_rules(symbol, snapshot, rulebook, current_regime)
    if not matches:
        _last_rejection_reason[symbol] = "No matching rule"
        return None

    matched_rule = matches[0]
    direction = matched_rule["direction"]
    template = matched_rule.get("template", "standard")
    total_score = _rule_match_score(matched_rule)
    long_total = total_score if direction == "LONG" else 0.0
    short_total = total_score if direction == "SHORT" else 0.0

    if state and is_whipsaw(state, symbol, direction):
        dbg.debug(f"[{symbol}] REJECTED: whipsaw detected (direction change <5min)")
        _last_rejection_reason[symbol] = "Whipsaw"
        return None

    tp_sl = _suggest_tp_sl_for_setup(candles_1h, direction, matched_rule)
    rule_filter = matched_rule.get("filter", {})
    rule_details = {
        "matched_rule": matched_rule["name"],
        "conditions": matched_rule["conditions"],
        "template": template,
        "filter": rule_filter,
        "test_stats": matched_rule.get("test_stats", {}),
        "train_stats": matched_rule.get("train_stats", {}),
        "candidate_count": len(matches),
        "rulebook_path": resolved_path,
    }

    return _emit_signal({
        "symbol": symbol,
        "direction": direction,
        "score": total_score,
        "entry_price": tp_sl["entry_price"],
        "tp": tp_sl["suggested_tp"],
        "sl": tp_sl["suggested_sl"],
        "tp_pct": tp_sl["tp_pct"],
        "sl_pct": tp_sl["sl_pct"],
        "atr": tp_sl["atr"],
        "sl_atr_mult": tp_sl["sl_atr_mult"],
        "rr_ratio": tp_sl["rr_ratio"],
        "technical_score": 0.0,
        "long_score": long_total,
        "short_score": short_total,
        "regime": "rule_match",
        "strategy": f"rule_{template}",
        "details": {"regime": "rule_match", "strategy": f"rule_{template}", "template": template},
        "signal_engine": signal_engine,
        # Legacy field aliases kept for backward compat with downstream consumers
        "signal_model": signal_engine,
        "statistical_setup": matched_rule["name"],
        "statistical_score": total_score,
        "statistical_details": rule_details,
    })

def _generate_ta_score_signal(
    symbol: str,
    candles_1h: list,
    state: dict = None,
    current_time: Optional[datetime] = None,
    precomputed_indicators: dict = None,
) -> Optional[dict]:
    """Score the current candle with TA indicators and return a signal if above threshold."""
    tech = score_technical(symbol, candles_1h, precomputed_indicators=precomputed_indicators)
    if tech["direction"] == "NEUTRAL":
        return None

    tech_long = tech["long_score"]
    tech_short = tech["short_score"]
    long_total = round(tech_long, 2)
    short_total = round(tech_short, 2)

    dbg.debug(f"[{symbol}] Score breakdown | Tech L={tech_long:.2f} S={tech_short:.2f}")
    dbg.debug(f"[{symbol}] Totals | LONG={long_total:.2f} SHORT={short_total:.2f}")

    if long_total < 1.0 and short_total < 1.0:
        if tech_long > 1.0 or tech_short > 1.0:
            dbg.debug(f"[{symbol}] REJECTED: both totals < 1.0 | tech was L={tech_long:.1f} S={tech_short:.1f}")
            _last_rejection_reason[symbol] = f"Weak (L={long_total:.1f} S={short_total:.1f})"
        else:
            _last_rejection_reason[symbol] = "No Signal"
        return None

    min_score_differential = 1.0
    score_gap = abs(long_total - short_total)
    if score_gap < min_score_differential:
        dbg.debug(f"[{symbol}] REJECTED: ambiguous signal | L={long_total:.2f} S={short_total:.2f} gap={score_gap:.2f} < {min_score_differential}")
        _last_rejection_reason[symbol] = f"Ambiguous (L={long_total:.1f} S={short_total:.1f})"
        return None

    if long_total >= short_total:
        direction = "LONG"
        total_score = long_total
        tech_component = tech_long
    else:
        direction = "SHORT"
        total_score = short_total
        tech_component = tech_short

    if state and is_whipsaw(state, symbol, direction):
        dbg.debug(f"[{symbol}] REJECTED: whipsaw detected (direction change <5min)")
        _last_rejection_reason[symbol] = "Whipsaw"
        return None

    strategy = tech["details"].get("strategy", "trend_pullback")
    regime = tech["details"].get("regime", "trending")
    tp_sl = _suggest_tp_sl_for_strategy(candles_1h, direction, strategy)

    if _is_asia_session(current_time):
        hour_utc = (current_time if current_time is not None else datetime.now(timezone.utc)).hour
        dbg.debug(f"[{symbol}] FILTERED: Asia session hour {hour_utc} UTC (low liquidity)")
        _last_rejection_reason[symbol] = f"Asia session (UTC {hour_utc})"
        return None

    return _emit_signal({
        "symbol": symbol,
        "direction": direction,
        "score": total_score,
        "entry_price": tp_sl["entry_price"],
        "tp": tp_sl["suggested_tp"],
        "sl": tp_sl["suggested_sl"],
        "tp_pct": tp_sl["tp_pct"],
        "sl_pct": tp_sl["sl_pct"],
        "atr": tp_sl["atr"],
        "sl_atr_mult": tp_sl["sl_atr_mult"],
        "rr_ratio": tp_sl["rr_ratio"],
        "technical_score": round(tech_component, 2),
        "long_score": long_total,
        "short_score": short_total,
        "regime": regime,
        "strategy": strategy,
        "details": tech["details"],
        "signal_engine": "ta_score",
        "signal_model": "ta_score",  # legacy alias
    })

def _generate_combined_signal(
    symbol: str,
    candles_1h: list,
    state: dict = None,
    current_time: Optional[datetime] = None,
    precomputed_indicators: dict = None,
    rulebook_path: Optional[str] = None,
    current_regime: Optional[str] = None,
) -> Optional[dict]:
    """Run rule_match and ta_score in parallel; prefer rule_match when it fires.

    Priority:
      1. Rule match — if a rulebook rule matches, use it (bypasses score threshold).
      2. TA score — fallback if no rule fires.
    """
    _last_rejection_reason.pop(symbol, None)

    rule_signal = _generate_rule_match_signal(
        symbol,
        candles_1h,
        "rule_match",
        state=state,
        current_time=current_time,
        precomputed_indicators=precomputed_indicators,
        rulebook_path=rulebook_path,
        current_regime=current_regime,
    )
    rule_reason = _last_rejection_reason.get(symbol)
    _last_rejection_reason.pop(symbol, None)

    ta_signal = _generate_ta_score_signal(
        symbol,
        candles_1h,
        state=state,
        current_time=current_time,
        precomputed_indicators=precomputed_indicators,
    )
    ta_reason = _last_rejection_reason.get(symbol)

    selected_signal = rule_signal or ta_signal
    if selected_signal is None:
        _last_rejection_reason[symbol] = (
            f"Hybrid rejected (statistical: {rule_reason or 'no matching rule'}; "
            f"technical: {ta_reason or 'no signal'})"
        )
        return None

    selected_source = "statistical" if rule_signal else "technical"
    if rule_signal:
        selected_reason = "rule matched"
    else:
        selected_reason = f"technical fallback ({rule_reason or 'no matching rule'})"
    result = dict(selected_signal)
    result["signal_engine"] = "combined"
    result["signal_model"] = "combined"  # legacy alias
    result["hybrid_details"] = {
        "statistical": None if not rule_signal else {
            "direction": rule_signal["direction"],
            "setup": rule_signal.get("statistical_setup"),
            "conditions": rule_signal.get("statistical_details", {}).get("conditions"),
            "template": rule_signal.get("statistical_details", {}).get("template"),
        },
        "statistical_reject_reason": rule_reason if not rule_signal else None,
        "technical": None if not ta_signal else {
            "direction": ta_signal["direction"],
            "score": ta_signal["score"],
            "long_score": ta_signal.get("long_score", 0.0),
            "short_score": ta_signal.get("short_score", 0.0),
            "strategy": ta_signal.get("strategy"),
        },
        "technical_reject_reason": ta_reason if not ta_signal else None,
        "selected": {"source": selected_source, "reason": selected_reason},
    }
    return _emit_signal(result)

# Backward-compat aliases for callers that still reference old function names
_generate_statistical_signal = _generate_rule_match_signal
_generate_technical_signal = _generate_ta_score_signal
_generate_hybrid_technical_statistical_signal = _generate_combined_signal

def generate_signal(symbol: str, candles_1h: list,
                    state: dict = None,
                    current_time: Optional[datetime] = None,
                    precomputed_indicators: dict = None,
                    signal_engine: Optional[SignalEngine] = None,
                    rulebook_path: Optional[str] = None,
                    current_regime: Optional[MarketRegime] = None,
                    # Backward-compat param aliases — callers using old names still work
                    signal_model: Optional[SignalEngine] = None,
                    validated_setups_path: Optional[str] = None) -> Optional[dict]:
    """Generate a complete trade signal from candle data.

    Args:
        symbol: Trading pair (e.g. "BTCUSDT")
        candles_1h: OHLCV candles (closed candles only)
        signal_engine: "ta_score" | "rule_match" | "combined" (default: DEFAULT_SIGNAL_ENGINE)
        rulebook_path: Path to validated rules JSON (rule_match / combined engines only)
        current_regime: BTC regime "bull"|"bear"|"chop"; gates regime-scoped rules.
            None = no regime filtering (backward compat).
    """
    raw = signal_engine or signal_model or DEFAULT_SIGNAL_ENGINE
    resolved = _ENGINE_COMPAT_ALIASES.get(raw.strip().lower(), raw.strip().lower())
    if resolved not in VALID_SIGNAL_ENGINES:
        resolved = "ta_score"
    rbook = rulebook_path or validated_setups_path

    if resolved == "rule_match":
        return _generate_rule_match_signal(
            symbol, candles_1h, resolved,
            state=state, current_time=current_time,
            precomputed_indicators=precomputed_indicators,
            rulebook_path=rbook, current_regime=current_regime,
        )
    if resolved == "combined":
        return _generate_combined_signal(
            symbol, candles_1h,
            state=state, current_time=current_time,
            precomputed_indicators=precomputed_indicators,
            rulebook_path=rbook, current_regime=current_regime,
        )
    return _generate_ta_score_signal(
        symbol, candles_1h,
        state=state, current_time=current_time,
        precomputed_indicators=precomputed_indicators,
    )
