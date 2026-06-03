"""Signals package — produces trade signals from candle data.

This is the brain of the trading system. `generate_signal()` is the single
source of truth used by both the live trader and the backtester.

Submodules:
  config    — strategy parameters and constants (SYMBOLS, thresholds, paths)
  filters   — whipsaw detection, Asia-session filter, rejection-reason cache
  snapshot  — _build_indicator_snapshot, precompute_indicators_for_all_candles
  scoring   — TA scoring (trend pullback, breakout) + regime detection + TP/SL
  rulebook  — load_rulebook, rule matching, scope filtering
  engine    — generate_signal + per-engine implementations

Top-level re-exports keep `from trading_bot.signals import generate_signal`
working for callers that don't need submodule granularity.
"""

from .config import (
    SYMBOLS,
    SIGNAL_THRESHOLD_TREND,
    SIGNAL_THRESHOLD_BREAKOUT,
    MAX_OPEN_POSITIONS,
    SIGNAL_COOLDOWN_CANDLES,
    RISK_PER_TRADE_PCT,
    VALID_SIGNAL_ENGINES,
    _ENGINE_COMPAT_ALIASES,
    RULEBOOK_PATH,
    CURATED_RULEBOOK_PATH,
    DEFAULT_SIGNAL_ENGINE,
)
from .filters import (
    _last_rejection_reason,
    is_whipsaw,
    mark_signal,
    _is_asia_session,
)
from .rulebook import (
    load_rulebook,
    load_validated_setups,
    _rule_match_sort_key,
    _rule_match_score,
    _resolve_rulebook_path,
    _rule_specificity,
    _rule_matches_context,
    _setup_scope_specificity,
    _setup_matches_scope,
    _find_matching_rules,
    _find_matching_statistical_setups,
    _suggest_tp_sl_for_setup,
    _suggest_tp_sl_for_strategy,
)
from .scoring import (
    detect_regime,
    score_trend_pullback,
    score_breakout,
    suggest_tp_sl,
    score_technical,
)
from .snapshot import (
    precompute_indicators_for_all_candles,
    _build_indicator_snapshot,
)
from .engine import (
    generate_signal,
    _emit_signal,
    _generate_rule_match_signal,
    _generate_ta_score_signal,
    _generate_combined_signal,
    _generate_statistical_signal,
    _generate_technical_signal,
    _generate_hybrid_technical_statistical_signal,
)


__all__ = [
    "generate_signal",
    "load_rulebook",
    "score_technical",
    "suggest_tp_sl",
    "SYMBOLS",
    "SIGNAL_THRESHOLD_TREND",
    "SIGNAL_THRESHOLD_BREAKOUT",
    "MAX_OPEN_POSITIONS",
    "SIGNAL_COOLDOWN_CANDLES",
    "RISK_PER_TRADE_PCT",
    "VALID_SIGNAL_ENGINES",
    "RULEBOOK_PATH",
    "CURATED_RULEBOOK_PATH",
    "DEFAULT_SIGNAL_ENGINE",
]
