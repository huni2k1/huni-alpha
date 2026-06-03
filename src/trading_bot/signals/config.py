"""Strategy parameters and constants shared by trader, backtester, and analyzer."""

from __future__ import annotations

import os


# Score thresholds — minimum signal score required to fire a trade.
SIGNAL_THRESHOLD_TREND = 7.0
SIGNAL_THRESHOLD_BREAKOUT = 6.0

MAX_OPEN_POSITIONS = 3
SIGNAL_COOLDOWN_CANDLES = 48  # 1h candles between signals per symbol
RISK_PER_TRADE_PCT = 1.5

# Canonical engine names accepted by generate_signal().
VALID_SIGNAL_ENGINES = {"ta_score", "rule_match", "combined"}

# Legacy aliases: old env-var / CLI values map to current engine names.
_ENGINE_COMPAT_ALIASES: dict[str, str] = {
    "technical":                        "ta_score",
    "statistical":                      "rule_match",
    "statistical_curated":              "rule_match",
    "statistical_wide_short_rsi28":     "rule_match",
    "combined_validated_rulebook":      "combined",
    "hybrid_technical_statistical":     "combined",
}

# Locate rulebooks relative to the package root (one level up from signals/).
_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RULEBOOK_PATH = os.environ.get(
    "VALIDATED_SETUPS_PATH",
    os.path.join(_PACKAGE_DIR, "validated_setups.json"),
)
CURATED_RULEBOOK_PATH = os.environ.get(
    "CURATED_VALIDATED_SETUPS_PATH",
    os.path.join(_PACKAGE_DIR, "curated_statistical_setups.json"),
)

DEFAULT_SIGNAL_ENGINE = _ENGINE_COMPAT_ALIASES.get(
    os.environ.get("SIGNAL_MODEL", "ta_score").strip().lower(),
    "ta_score",
)

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT",
]
