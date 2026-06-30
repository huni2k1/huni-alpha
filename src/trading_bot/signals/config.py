"""Strategy parameters and constants shared by trader, backtester, and analyzer."""

from __future__ import annotations

import os


# Score thresholds — minimum signal score required to fire a trade.
SIGNAL_THRESHOLD_TREND = 7.0
SIGNAL_THRESHOLD_BREAKOUT = 6.0

MAX_OPEN_POSITIONS = 8
# Minimum candles between two signals on the same symbol. Used by the live
# trader, backtester, and analyzer. Each system reads from here so the gate
# stays consistent across all three. Anchoring (entry vs exit) is up to the
# caller — live anchors at exit, backtester/analyzer at signal close.
SIGNAL_COOLDOWN_CANDLES = 24
RISK_PER_TRADE_PCT = 1.5

# Time per candle for each Binance interval. Used to convert a candle-count
# cooldown into milliseconds when comparing wall-clock timestamps.
INTERVAL_MS: dict[str, int] = {
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def cooldown_ms(interval: str, candles: int = SIGNAL_COOLDOWN_CANDLES) -> int:
    """Cooldown duration in milliseconds for a given candle interval."""
    return candles * INTERVAL_MS.get(interval, INTERVAL_MS["1h"])

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
