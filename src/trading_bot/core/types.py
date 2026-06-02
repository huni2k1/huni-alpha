"""Shared type aliases and data shapes — the vocabulary of the trading bot.

- `Literal` aliases (Direction, MarketRegime, …) are zero-cost type hints that
  restrict valid string values. Catch typos at parse time; document the full
  set of valid values in one place.
- Frozen dataclasses (Candle, …) define the data shapes that flow through the
  pipeline. Built and read by typed code; callers may still use raw dicts
  during the migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


# Trade direction. Signal scoring may also produce "NEUTRAL", but that gets
# filtered before becoming a real signal — by the time a Direction reaches
# execution code, it's only LONG or SHORT.
Direction = Literal["LONG", "SHORT"]

# BTC-derived market regime (see regime.py).
MarketRegime = Literal["bull", "bear", "chop", "unknown"]

# Canonical signal engines. Scanner accepts a handful of aliases
# (e.g. "combined_validated_rulebook" → "combined") which resolve to these.
SignalEngine = Literal["ta_score", "rule_match", "combined"]

# Why a position exited.
ExitReason = Literal["TP", "SL", "TIMEOUT", "TRAIL_SL", "UNKNOWN"]

# TP/SL template (rulebook setups carry one of these).
Template = Literal["standard", "wide"]

# Same-candle TP/SL tie-break policy (see core/tp_sl + execution/fills).
TieBreak = Literal["random", "sl"]


# ─────────────────────────────────────────────────────────────────
# Data shapes
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candle:
    """One normalized OHLCV candle.

    Frozen so it's hashable and can't drift after construction. Callers in
    the migration may still use raw dicts; `from_dict` / `from_ohlcv_list`
    convert from the historical formats.
    """

    open: float
    high: float
    low: float
    close: float
    volume: float
    open_time: Optional[int] = None   # ms since epoch
    close_time: Optional[int] = None  # ms since epoch
    symbol: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Candle":
        """Build from a candle dict (analyzer/backtester style)."""
        return cls(
            open=float(d["open"]),
            high=float(d["high"]),
            low=float(d["low"]),
            close=float(d["close"]),
            volume=float(d.get("volume", 0.0)),
            open_time=int(d["open_time"]) if d.get("open_time") is not None else None,
            close_time=int(d["close_time"]) if d.get("close_time") is not None else None,
            symbol=d.get("symbol"),
        )

    @classmethod
    def from_ohlcv_list(
        cls,
        values: list,
        *,
        open_time: Optional[int] = None,
        close_time: Optional[int] = None,
        symbol: Optional[str] = None,
    ) -> "Candle":
        """Build from a Binance-style list: [open, high, low, close, volume, ...]."""
        return cls(
            open=float(values[0]),
            high=float(values[1]),
            low=float(values[2]),
            close=float(values[3]),
            volume=float(values[4]) if len(values) > 4 else 0.0,
            open_time=open_time,
            close_time=close_time,
            symbol=symbol,
        )

    def to_dict(self) -> dict:
        """Round-trip back to the dict shape used by the rest of the codebase."""
        return {
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "symbol": self.symbol,
        }


@dataclass(frozen=True)
class Signal:
    """Complete output of generate_signal — everything needed to size + place a trade.

    Frozen, but engine-specific extras (details / statistical_details /
    hybrid_details) are stored as plain dicts to keep the migration shallow.
    Future PRs may replace those with typed sub-records.

    The `regime` field carries the SIGNAL's notion of regime (which can be
    a scanner-internal label like "rule_match" or "trending"), not the
    BTC-derived MarketRegime. The two overlap but are not identical — hence
    `regime: str` here, not `regime: MarketRegime`.
    """

    symbol: str
    direction: Direction
    score: float

    # Entry + exits (computed at signal time; backtester/trader recompute at fill).
    entry_price: float
    tp: float
    sl: float
    tp_pct: float
    sl_pct: float

    # TP/SL recipe — lets callers recompute fresh at the actual fill price.
    atr: float
    sl_atr_mult: float
    rr_ratio: float

    # Scoring breakdown.
    long_score: float = 0.0
    short_score: float = 0.0
    technical_score: float = 0.0

    # Provenance.
    signal_engine: SignalEngine = "ta_score"
    strategy: str = "trend_pullback"
    regime: str = "trending"

    # Engine-specific extras (dicts for now).
    details: dict = field(default_factory=dict)
    statistical_setup: Optional[str] = None
    statistical_details: Optional[dict] = None
    hybrid_details: Optional[dict] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Signal":
        """Build from the dict shape that generate_signal currently returns."""
        return cls(
            symbol=d["symbol"],
            direction=d["direction"],
            score=float(d["score"]),
            entry_price=float(d["entry_price"]),
            tp=float(d["tp"]),
            sl=float(d["sl"]),
            tp_pct=float(d["tp_pct"]),
            sl_pct=float(d["sl_pct"]),
            atr=float(d.get("atr", 0.0) or 0.0),
            sl_atr_mult=float(d.get("sl_atr_mult", 1.5) or 1.5),
            rr_ratio=float(d.get("rr_ratio", 2.0) or 2.0),
            long_score=float(d.get("long_score", 0.0) or 0.0),
            short_score=float(d.get("short_score", 0.0) or 0.0),
            technical_score=float(d.get("technical_score", 0.0) or 0.0),
            signal_engine=d.get("signal_engine", "ta_score"),
            strategy=d.get("strategy", "trend_pullback"),
            regime=d.get("regime", "trending"),
            details=dict(d.get("details") or {}),
            statistical_setup=d.get("statistical_setup"),
            statistical_details=d.get("statistical_details"),
            hybrid_details=d.get("hybrid_details"),
        )

    def to_dict(self) -> dict:
        """Round-trip for callers that still expect dict access.

        Also writes `signal_model` as a legacy alias for `signal_engine` so
        legacy consumers reading either key continue to work.
        """
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "score": self.score,
            "entry_price": self.entry_price,
            "tp": self.tp,
            "sl": self.sl,
            "tp_pct": self.tp_pct,
            "sl_pct": self.sl_pct,
            "atr": self.atr,
            "sl_atr_mult": self.sl_atr_mult,
            "rr_ratio": self.rr_ratio,
            "long_score": self.long_score,
            "short_score": self.short_score,
            "technical_score": self.technical_score,
            "signal_engine": self.signal_engine,
            "signal_model": self.signal_engine,  # legacy alias
            "strategy": self.strategy,
            "regime": self.regime,
            "details": dict(self.details),
            "statistical_setup": self.statistical_setup,
            "statistical_details": self.statistical_details,
            "hybrid_details": self.hybrid_details,
        }
