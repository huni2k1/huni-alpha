"""Compatibility shim — re-exports the API previously lived in this file.

scanner.py was carved up into:
  signals/        — engine, scoring, snapshot, rulebook, filters, config
  binance_http.py — HTTP, Binance klines, Telegram
  logging_setup.py — logger + configure_logging

This shim keeps `from trading_bot import scanner` working while consumers
migrate to the new import paths. New code should import directly from
trading_bot.signals / trading_bot.binance_http / trading_bot.logging_setup.
"""

# Indicator math (legacy re-export — new code should use core.indicators directly)
from .core.indicators import (
    adx,
    atr,
    bollinger,
    bollinger_bandwidth,
    ema,
    ema_alignment,
    macd,
    market_structure,
    rsi,
    support_resistance,
    volume_ratio,
)

# Logging
from .logging_setup import (
    LOG_FILE,
    DEBUG_LOG_FILE,
    log,
    dbg,
    configure_logging,
)

# HTTP / Binance / Telegram
from .binance_http import (
    SESSION,
    TELEGRAM_TOKEN,
    TELEGRAM_CHAT,
    get,
    fetch_klines,
    fetch_klines_cached,
    send_telegram,
)

# Signal logic and constants
from .signals import (
    # config
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
    # filters
    _last_rejection_reason,
    is_whipsaw,
    mark_signal,
    _is_asia_session,
    # rulebook
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
    # scoring
    detect_regime,
    score_trend_pullback,
    score_breakout,
    suggest_tp_sl,
    score_technical,
    # snapshot
    precompute_indicators_for_all_candles,
    _build_indicator_snapshot,
    # engine
    generate_signal,
    _emit_signal,
    _generate_rule_match_signal,
    _generate_ta_score_signal,
    _generate_combined_signal,
    _generate_statistical_signal,
    _generate_technical_signal,
    _generate_hybrid_technical_statistical_signal,
)

# Re-export commonly-used stdlib modules so legacy tests that do
# `scanner.time` / `scanner.os` / etc keep resolving.
import os
import sys
import time
import json
import logging
import requests
import numpy as np

# Re-export internal submodules for legacy access
from . import candle_cache, setup_conditions
from .signals import rulebook as _rulebook_module

# Legacy module-level globals that some tests poke at directly.
_rulebook_cache = _rulebook_module._rulebook_cache
_rulebook_missing_warned = _rulebook_module._rulebook_missing_warned
