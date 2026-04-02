#!/usr/bin/env python3
"""
Statistical setup analyzer for the trading bot.

Measures which indicator conditions and combinations have historical edge
using the bot's existing candle data, indicators, next-open entry model,
and ATR-based TP/SL logic.

Key design decisions:
  1. Single flat condition list — conditions are factual statements about
     the candle and are tested against both long and short outcomes.
  2. Non-overlapping trade selection at evaluation time so the selected
     outcomes are less correlated and the statistical tests are stricter.
  3. Baselines use the same matched sampling logic as the tested subset.
  4. Walk-forward validation can include per-symbol breakdown for any
     setup that survives out-of-sample filters.
  5. Runtime-sensitive row building stays optimized with precomputed
     indicators, pinned end dates, and optional parallel workers.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Callable, Optional

try:
    from . import scanner
    from .backtester import fetch_klines_historical_cached
    from .setup_conditions import ALL_CONDITIONS, matches_conditions, normalize_conditions
    from .statistical_utils import (
        SampleStats,
        build_walk_forward_windows,
        summarize_outcomes,
        two_proportion_ztest,
    )
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scanner  # type: ignore
    from backtester import fetch_klines_historical_cached  # type: ignore
    from setup_conditions import ALL_CONDITIONS, matches_conditions, normalize_conditions  # type: ignore
    from statistical_utils import (  # type: ignore
        SampleStats,
        build_walk_forward_windows,
        summarize_outcomes,
        two_proportion_ztest,
    )


log = logging.getLogger("setup_analyzer")
if not log.handlers:
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(handler)

DEFAULT_SYMBOLS = list(getattr(scanner, "SYMBOLS", [])) or [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "DOTUSDT",
]
DEFAULT_REPORT_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_analysis_report.json")
DEFAULT_VALIDATED_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "validated_setups.json")
DEFAULT_WARMUP_CANDLES = 1000
DEFAULT_MAX_HOLDING_CANDLES = 180
DEFAULT_MIN_TRAIN_TRADES = 30
DEFAULT_MIN_TEST_TRADES = 15
DEFAULT_PREFILTER_P = 0.10
DEFAULT_VALIDATION_P = 0.05
DEFAULT_FEE_PCT = 0.10
DEFAULT_SLIPPAGE_PCT = 0.05
MAX_COMBO_TESTS_DEFAULT = 50
MAX_CANDIDATE_CONDITIONS = 8
EVAL_COOLDOWN_CANDLES = 48
DEFAULT_DISCOVERY_VARIANTS = ("pooled",)
VALID_DISCOVERY_VARIANTS = ("pooled", "symbol_specific")
DOMINANCE_THRESHOLDS = {
    "train_avg_pnl_pct": 0.15,
    "test_avg_pnl_pct": 0.15,
    "train_profit_factor": 0.15,
    "test_profit_factor": 0.15,
    "test_edge_win_rate": 3.0,
}

SETUP_TEMPLATES = {
    "standard": {"sl_atr_mult": 1.5, "rr_ratio": 2.0},
    "wide": {"sl_atr_mult": 2.0, "rr_ratio": 2.5},
}
LEGACY_TEMPLATE_ALIASES = {
    "trend": "standard",
    "breakout": "wide",
}


def _normalize_template_name(template_name: str) -> str:
    """Map legacy template names onto the current runtime terminology."""
    return LEGACY_TEMPLATE_ALIASES.get(str(template_name), str(template_name))
def candles_to_ohlcv_lists(candles: list[dict]) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    """Convert candle dicts into parallel OHLCV lists."""
    opens = [float(c["open"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]
    return opens, highs, lows, closes, volumes


def calculate_atr(candles: list[dict], period: int = 20) -> float:
    """ATR matching the scanner's TP/SL logic."""
    if len(candles) < 2:
        return 0.0

    _, highs, lows, closes, _ = candles_to_ohlcv_lists(candles)
    tr_values = []
    for i in range(1, len(closes)):
        tr_values.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    if not tr_values:
        return 0.0
    return sum(tr_values[-period:]) / min(period, len(tr_values))


def _build_adx_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float]:
    """Build a per-candle ADX series using the scanner's Wilder-style approach."""
    series = [0.0] * len(closes)
    if len(highs) < period * 2:
        return series

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(highs)):
        h_diff = highs[i] - highs[i - 1]
        l_diff = lows[i - 1] - lows[i]
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0.0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0.0)
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    def wilder(values: list[float], p: int) -> list[float]:
        result = [sum(values[:p])]
        for value in values[p:]:
            result.append(result[-1] - result[-1] / p + value)
        return result

    atr_s = wilder(trs, period)
    pdm_s = wilder(plus_dm, period)
    mdm_s = wilder(minus_dm, period)
    dx_values = []
    for atr_val, plus_val, minus_val in zip(atr_s, pdm_s, mdm_s):
        if atr_val == 0:
            dx_values.append(0.0)
            continue
        pdi = 100.0 * plus_val / atr_val
        mdi = 100.0 * minus_val / atr_val
        dx_values.append(100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0)

    start_idx = period
    for offset, candle_idx in enumerate(range(start_idx, len(closes))):
        window = dx_values[max(0, offset - period + 1):offset + 1]
        series[candle_idx] = float(sum(window) / len(window)) if window else 0.0
    return series


def _build_bollinger_context(
    closes: list[float],
    period: int = 20,
    std_mult: float = 2.0,
    squeeze_lookback: int = 100,
) -> dict:
    """Precompute per-candle Bollinger values and squeeze flags."""
    mids = [0.0] * len(closes)
    uppers = [0.0] * len(closes)
    lowers = [0.0] * len(closes)
    bandwidths = [5.0] * len(closes)
    squeezes = [False] * len(closes)

    for idx in range(period - 1, len(closes)):
        window = closes[idx - period + 1:idx + 1]
        mid = float(sum(window) / len(window))
        std = float(scanner.np.std(window))
        upper = mid + std_mult * std
        lower = mid - std_mult * std
        bw = (upper - lower) / mid * 100.0 if mid > 0 else 5.0
        mids[idx] = mid
        uppers[idx] = upper
        lowers[idx] = lower
        bandwidths[idx] = bw
        history = [value for value in bandwidths[max(period - 1, idx - squeeze_lookback):idx] if value is not None]
        if history:
            squeezes[idx] = bw < float(scanner.np.percentile(history, 20))

    return {
        "bb_mid": mids,
        "bb_upper": uppers,
        "bb_lower": lowers,
        "bb_bandwidth": bandwidths,
        "bb_squeeze": squeezes,
    }


def _build_volume_ratio_series(volumes: list[float], period: int = 20) -> list[float]:
    """Precompute volume ratio for every candle."""
    series = [1.0] * len(volumes)
    for idx in range(period, len(volumes)):
        prior = volumes[idx - period:idx]
        avg = sum(prior) / len(prior) if prior else 0.0
        series[idx] = volumes[idx] / avg if avg > 0 else 1.0
    return series


def precompute_symbol_context(candles: list[dict]) -> dict:
    """Precompute reusable indicator context for one symbol."""
    ohlcv = [[c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in candles]
    _, highs, lows, closes, volumes = candles_to_ohlcv_lists(candles)
    cheap = scanner.precompute_indicators_for_all_candles(ohlcv)
    return {
        "ohlcv": ohlcv,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "volumes": volumes,
        "cheap": cheap,
        "adx": _build_adx_series(highs, lows, closes),
        "vol_ratio": _build_volume_ratio_series(volumes),
        "bb": _build_bollinger_context(closes),
    }


def compute_indicator_snapshot(symbol: str, candles: list[dict], idx: int, context: Optional[dict] = None) -> dict:
    """Compute all analyzer indicators for the candle at idx using history up to idx."""
    window = candles[:idx + 1]
    if context is None:
        context = precompute_symbol_context(candles)

    cheap = context["cheap"].get(idx, {})
    closes = context["closes"][:idx + 1]
    highs = context["highs"][:idx + 1]
    lows = context["lows"][:idx + 1]
    bb_ctx = context["bb"]
    ohlcv_window = context["ohlcv"][max(0, idx - 5):idx + 1]
    higher_highs, lower_lows = scanner.market_structure(ohlcv_window)
    candle_time = datetime.fromtimestamp(window[-1]["close_time"] / 1000, tz=timezone.utc)

    return {
        "symbol": symbol,
        "idx": idx,
        "open_time": int(window[-1]["open_time"]),
        "close_time": int(window[-1]["close_time"]),
        "month": candle_time.strftime("%Y-%m"),
        "hour_utc": candle_time.hour,
        "open": float(window[-1]["open"]),
        "high": float(window[-1]["high"]),
        "low": float(window[-1]["low"]),
        "close": float(window[-1]["close"]),
        "volume": float(window[-1]["volume"]),
        "rsi": float(cheap.get("rsi", scanner.rsi(closes))),
        "e9": float(cheap.get("e9") or 0.0),
        "e21": float(cheap.get("e21") or 0.0),
        "e50": float(cheap.get("e50") or 0.0),
        "e200": float(cheap.get("e200") or 0.0),
        "bull_ema_align": bool(cheap.get("bull_align", False)),
        "bear_ema_align": bool(cheap.get("bear_align", False)),
        "above_ema200": bool(cheap.get("above_e200", False)),
        "below_ema200": bool(cheap.get("below_e200", False)),
        "macd_line": float(cheap.get("macd_line", 0.0)),
        "macd_signal": float(cheap.get("macd_signal", 0.0)),
        "macd_hist": float(cheap.get("macd_hist", 0.0)),
        "macd_hist_prev": float(cheap.get("macd_hist_prev", 0.0)),
        "adx": float(context["adx"][idx]),
        "vol_ratio": float(context["vol_ratio"][idx]),
        "bb_mid": float(bb_ctx["bb_mid"][idx]),
        "bb_upper": float(bb_ctx["bb_upper"][idx]),
        "bb_lower": float(bb_ctx["bb_lower"][idx]),
        "bb_bandwidth": float(bb_ctx["bb_bandwidth"][idx]),
        "bb_squeeze": bool(bb_ctx["bb_squeeze"][idx]),
        "higher_highs": bool(higher_highs),
        "lower_lows": bool(lower_lows),
        "atr20": float(calculate_atr(window, 20)),
    }


def resolve_same_candle_hit(candle: dict, direction: str, entry_price: float) -> str:
    """
    Resolve TP/SL conflicts inside a single candle.

    Uses the same open-based heuristic as the backtester, but breaks exact-open
    ties conservatively toward SL so research runs stay deterministic.
    """
    candle_open = float(candle["open"])
    if direction == "LONG":
        if candle_open > entry_price:
            return "TP"
        return "SL"

    if candle_open < entry_price:
        return "TP"
    return "SL"


def simulate_forward_trade(
    candles: list[dict],
    signal_idx: int,
    direction: str,
    sl_atr_mult: float,
    rr_ratio: float,
    max_holding_candles: int = DEFAULT_MAX_HOLDING_CANDLES,
    fee_pct: float = DEFAULT_FEE_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> Optional[dict]:
    """Simulate a next-open entry forward until TP, SL, or timeout."""
    entry_idx = signal_idx + 1
    if entry_idx >= len(candles):
        return None

    atr = calculate_atr(candles[:signal_idx + 1], 20)
    if atr <= 0:
        return None

    entry_candle = candles[entry_idx]
    entry_price = float(entry_candle["open"])
    if slippage_pct > 0:
        if direction == "LONG":
            entry_price *= 1.0 + slippage_pct / 100.0
        else:
            entry_price *= 1.0 - slippage_pct / 100.0

    sl_distance = atr * sl_atr_mult
    tp_distance = sl_distance * rr_ratio
    if direction == "LONG":
        tp_price = entry_price + tp_distance
        sl_price = entry_price - sl_distance
    else:
        tp_price = entry_price - tp_distance
        sl_price = entry_price + sl_distance

    last_idx = min(len(candles) - 1, entry_idx + max_holding_candles - 1)
    exit_reason = "TIMEOUT"
    exit_idx = last_idx
    exit_price = float(candles[last_idx]["close"])

    for idx in range(entry_idx, last_idx + 1):
        candle = candles[idx]
        high = float(candle["high"])
        low = float(candle["low"])
        if direction == "LONG":
            tp_hit = high >= tp_price
            sl_hit = low <= sl_price
        else:
            tp_hit = low <= tp_price
            sl_hit = high >= sl_price

        if tp_hit and sl_hit:
            exit_reason = resolve_same_candle_hit(candle, direction, entry_price)
            exit_idx = idx
            exit_price = tp_price if exit_reason == "TP" else sl_price
            break
        if tp_hit:
            exit_reason = "TP"
            exit_idx = idx
            exit_price = tp_price
            break
        if sl_hit:
            exit_reason = "SL"
            exit_idx = idx
            exit_price = sl_price
            break

    if slippage_pct > 0:
        if direction == "LONG":
            exit_price *= 1.0 - slippage_pct / 100.0
        else:
            exit_price *= 1.0 + slippage_pct / 100.0

    if direction == "LONG":
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
    else:
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100.0
    pnl_pct -= fee_pct

    return {
        "is_win": exit_reason == "TP",
        "exit_reason": exit_reason,
        "entry_idx": entry_idx,
        "exit_idx": exit_idx,
        "holding_candles": (exit_idx - entry_idx) + 1,
        "entry_price": round(entry_price, 8),
        "exit_price": round(exit_price, 8),
        "tp_price": round(tp_price, 8),
        "sl_price": round(sl_price, 8),
        "atr": round(atr, 8),
        "pnl_pct": round(pnl_pct, 4),
    }


def _outcome_key(template_name: str, direction: str) -> str:
    return f"{_normalize_template_name(template_name)}_{direction.lower()}"


def build_symbol_rows(
    symbol: str,
    candles: list[dict],
    analysis_start_ms: int,
    warmup_candles: int = DEFAULT_WARMUP_CANDLES,
    max_holding_candles: int = DEFAULT_MAX_HOLDING_CANDLES,
    fee_pct: float = DEFAULT_FEE_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> list[dict]:
    """Build analyzer rows for one symbol."""
    rows = []
    context = precompute_symbol_context(candles)
    start_idx = max(warmup_candles, 50)
    for idx in range(start_idx, len(candles) - 1):
        if candles[idx]["close_time"] < analysis_start_ms:
            continue

        snapshot = compute_indicator_snapshot(symbol, candles, idx, context=context)
        row = dict(snapshot)
        row["outcomes"] = {}
        for template_name, template in SETUP_TEMPLATES.items():
            for direction in ("LONG", "SHORT"):
                outcome = simulate_forward_trade(
                    candles=candles,
                    signal_idx=idx,
                    direction=direction,
                    sl_atr_mult=template["sl_atr_mult"],
                    rr_ratio=template["rr_ratio"],
                    max_holding_candles=max_holding_candles,
                    fee_pct=fee_pct,
                    slippage_pct=slippage_pct,
                )
                if outcome is not None:
                    row["outcomes"][_outcome_key(template_name, direction)] = outcome
        if row["outcomes"]:
            rows.append(row)
    return rows


def load_and_build_symbol_rows(
    symbol: str,
    start_ms: int,
    end_ms: int,
    analysis_start_ms: int,
    warmup_candles: int,
    max_holding_candles: int,
    fee_pct: float,
    slippage_pct: float,
) -> tuple[str, list[dict]]:
    """Load historical candles for one symbol and build analyzer rows."""
    candles = fetch_klines_historical_cached(symbol, "1h", start_ms, end_ms, use_cache=True)
    if not candles or len(candles) < warmup_candles + 100:
        log.warning(f"  {symbol}: insufficient candles ({len(candles) if candles else 0}), skipping")
        return symbol, []
    rows = build_symbol_rows(
        symbol=symbol,
        candles=candles,
        analysis_start_ms=analysis_start_ms,
        warmup_candles=warmup_candles,
        max_holding_candles=max_holding_candles,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
    )
    return symbol, rows


def evaluate_subset(
    rows: list[dict],
    template_name: str,
    direction: str,
    condition_names: list[str],
    baseline_stats: SampleStats,
    min_trades: int,
    cooldown: int = EVAL_COOLDOWN_CANDLES,
) -> Optional[dict]:
    """
    Evaluate one condition set with non-overlapping trade selection.

    The subset and baseline both use the same cooldown spacing so the
    comparison is on similarly sampled trades rather than raw row counts.
    """
    outcome_key = _outcome_key(template_name, direction)
    normalized_conditions = normalize_conditions(condition_names)
    matched_rows = []
    for row in rows:
        if outcome_key not in row["outcomes"]:
            continue
        if matches_conditions(row, normalized_conditions):
            matched_rows.append(row)

    matched_rows.sort(key=lambda row: (row.get("close_time", 0), row.get("symbol", "")))
    outcomes = _select_non_overlapping(matched_rows, outcome_key, cooldown)
    stats = summarize_outcomes(outcomes)
    if stats.trades < min_trades:
        return None

    p_value = two_proportion_ztest(stats.wins, stats.trades, baseline_stats.wins, baseline_stats.trades)
    edge_wr = stats.win_rate - baseline_stats.win_rate
    edge_pnl = stats.avg_pnl_pct - baseline_stats.avg_pnl_pct
    return {
        "conditions": normalized_conditions,
        "direction": direction,
        "template": template_name,
        "count": stats.trades,
        "win_rate": round(stats.win_rate, 2),
        "avg_pnl_pct": round(stats.avg_pnl_pct, 4),
        "pnl_sum_pct": round(stats.pnl_sum_pct, 4),
        "baseline_win_rate": round(baseline_stats.win_rate, 2),
        "baseline_avg_pnl_pct": round(baseline_stats.avg_pnl_pct, 4),
        "edge_win_rate": round(edge_wr, 2),
        "edge_avg_pnl_pct": round(edge_pnl, 4),
        "profit_factor": round(stats.profit_factor, 4)
        if stats.profit_factor != float("inf")
        else float("inf"),
        "p_value": round(p_value, 6),
        "wilson_low": round(stats.wilson_low * 100.0, 2),
        "wilson_high": round(stats.wilson_high * 100.0, 2),
    }


def _selection_anchor(row: dict) -> int:
    """Return the time anchor used for cooldown spacing."""
    if "close_time" in row:
        return int(row["close_time"])
    return int(row.get("idx", 0))


def _select_non_overlapping(
    rows: list[dict],
    outcome_key: str,
    cooldown: int = EVAL_COOLDOWN_CANDLES,
) -> list[dict]:
    """Select non-overlapping outcomes using per-symbol cooldown spacing."""
    selected = []
    next_allowed_anchor_by_symbol = {}
    use_time = bool(rows and rows[0].get("close_time") is not None)
    cooldown_step = cooldown * 3_600_000 if use_time else cooldown

    for row in rows:
        symbol = str(row.get("symbol", ""))
        anchor = _selection_anchor(row)
        if anchor < next_allowed_anchor_by_symbol.get(symbol, -1):
            continue
        outcome = row["outcomes"].get(outcome_key)
        if outcome is None:
            continue
        selected.append(outcome)
        next_allowed_anchor_by_symbol[symbol] = anchor + cooldown_step

    return selected


def _compute_baseline(
    rows: list[dict],
    template_name: str,
    direction: str,
    cooldown: int = EVAL_COOLDOWN_CANDLES,
) -> SampleStats:
    """Compute a matched baseline using the same non-overlapping sampling logic."""
    outcome_key = _outcome_key(template_name, direction)
    sorted_rows = sorted(
        [row for row in rows if outcome_key in row["outcomes"]],
        key=lambda row: (row.get("close_time", 0), row.get("symbol", "")),
    )
    outcomes = _select_non_overlapping(sorted_rows, outcome_key, cooldown)
    return summarize_outcomes(outcomes)


def analyze_conditions(
    rows: list[dict],
    template_name: str,
    direction: str,
    min_trades: int,
    combo_max_size: int,
    max_combo_tests: int,
    prefilter_p: float,
    cooldown: int = EVAL_COOLDOWN_CANDLES,
) -> tuple[dict, list[dict]]:
    """Analyze individual flat conditions and promising combinations."""
    baseline = _compute_baseline(rows, template_name, direction, cooldown)

    individual_results = []
    for name in ALL_CONDITIONS:
        result = evaluate_subset(rows, template_name, direction, [name], baseline, min_trades, cooldown)
        if result:
            individual_results.append(result)
    individual_results.sort(key=lambda item: (item["p_value"], -item["edge_avg_pnl_pct"]))

    promising = [
        result["conditions"][0]
        for result in individual_results
        if result["p_value"] <= prefilter_p
    ][:MAX_CANDIDATE_CONDITIONS]

    combo_results = []
    tested = 0
    for size in range(2, combo_max_size + 1):
        for combo in combinations(promising, size):
            if tested >= max_combo_tests:
                break
            result = evaluate_subset(rows, template_name, direction, list(combo), baseline, min_trades, cooldown)
            tested += 1
            if result:
                combo_results.append(result)
        if tested >= max_combo_tests:
            break

    combo_results.sort(key=lambda item: (item["p_value"], -item["edge_avg_pnl_pct"]))
    return {
        "baseline": baseline.to_dict(),
        "individual_conditions": individual_results,
        "combinations": combo_results,
    }, combo_results


def _window_subset(rows: list[dict], months: list[str]) -> list[dict]:
    return [row for row in rows if row["month"] in months]


def _setup_name(template_name: str, direction: str, conditions: list[str]) -> str:
    """Build a stable setup identifier from normalized conditions."""
    normalized_conditions = normalize_conditions(conditions)
    return f"{_normalize_template_name(template_name)}_{direction.lower()}_{'_'.join(normalized_conditions)}"


def _runtime_conditions_for_setup(setup: dict) -> list[str]:
    """Export setups exactly as validated, with no hidden condition injection."""
    return normalize_conditions(list(setup.get("conditions", [])))


def _export_runtime_setup(setup: dict) -> dict:
    """Return the production-facing statistical setup payload."""
    exported = {
        "name": setup["name"],
        "template": _normalize_template_name(setup["template"]),
        "direction": setup["direction"],
        "conditions": _runtime_conditions_for_setup(setup),
        "tp_sl": setup["tp_sl"],
        "train_stats": setup["train_stats"],
        "test_stats": setup["test_stats"],
        "by_symbol": setup["by_symbol"],
    }

    scope_type = setup.get("scope_type")
    scope_symbol = setup.get("scope_symbol")
    if scope_type == "symbol" and scope_symbol:
        exported["scope_key"] = setup.get("scope_key", f"symbol_{scope_symbol}")
        exported["scope_type"] = "symbol"
        exported["scope_symbol"] = scope_symbol

    return exported


def _per_symbol_breakdown(
    rows: list[dict],
    template_name: str,
    direction: str,
    condition_names: list[str],
    cooldown: int = EVAL_COOLDOWN_CANDLES,
) -> dict:
    """Break down a validated setup by symbol using the same sampling rules."""
    breakdown = {}
    for symbol in sorted({row["symbol"] for row in rows}):
        symbol_rows = [row for row in rows if row["symbol"] == symbol]
        if not symbol_rows:
            continue
        result = evaluate_subset(
            symbol_rows,
            template_name,
            direction,
            condition_names,
            _compute_baseline(symbol_rows, template_name, direction, cooldown),
            min_trades=1,
            cooldown=cooldown,
        )
        if result is None:
            continue
        breakdown[symbol] = {
            "trades": result["count"],
            "win_rate": result["win_rate"],
            "avg_pnl_pct": result["avg_pnl_pct"],
            "profit_factor": result["profit_factor"],
        }
    return breakdown


def _normalize_discovery_variants(discovery_variants: Optional[list[str]]) -> list[str]:
    """Return a stable, validated discovery-variant list."""
    variants = discovery_variants or list(DEFAULT_DISCOVERY_VARIANTS)
    normalized = []
    seen = set()
    for variant in variants:
        value = str(variant).strip().lower()
        if value not in VALID_DISCOVERY_VARIANTS or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized or list(DEFAULT_DISCOVERY_VARIANTS)


def build_analysis_views(
    rows: list[dict],
    symbols: list[str],
    discovery_variants: Optional[list[str]] = None,
) -> dict[str, dict]:
    """Build alternative research scopes for edge discovery."""
    variants = _normalize_discovery_variants(discovery_variants)
    symbol_order = symbols or sorted({row["symbol"] for row in rows})
    views = {}

    if "pooled" in variants:
        views["pooled"] = {
            "rows": rows,
            "template_names": sorted(SETUP_TEMPLATES),
            "scope": {"scope_key": "pooled", "scope_type": "pooled"},
        }

    if "symbol_specific" in variants:
        for symbol in symbol_order:
            subset = [row for row in rows if row["symbol"] == symbol]
            if not subset:
                continue
            key = f"symbol_{symbol}"
            views[key] = {
                "rows": subset,
                "template_names": sorted(SETUP_TEMPLATES),
                "scope": {"scope_key": key, "scope_type": "symbol", "scope_symbol": symbol},
            }

    return views


def validate_candidates(
    rows: list[dict],
    template_name: str,
    direction: str,
    candidate_results: list[dict],
    train_months: int,
    test_months: int,
    step_months: int,
    min_train_trades: int,
    min_test_trades: int,
    validation_p: float,
    cooldown: int = EVAL_COOLDOWN_CANDLES,
    scope_context: Optional[dict] = None,
) -> tuple[list[dict], list[dict]]:
    """Walk-forward validate candidate condition sets."""
    month_starts = [
        datetime.strptime(row["month"], "%Y-%m").replace(tzinfo=timezone.utc)
        for row in rows
    ]
    windows = build_walk_forward_windows(month_starts, train_months, test_months, step_months)
    if not windows:
        return [], []

    validated = []
    rejected = []
    for candidate in candidate_results:
        conditions = normalize_conditions(candidate["conditions"])
        window_results = []
        failed_reason = None
        saw_failure = False
        for window in windows:
            train_rows = _window_subset(rows, window["train_months"])
            test_rows = _window_subset(rows, window["test_months"])
            train_baseline = _compute_baseline(train_rows, template_name, direction, cooldown)
            test_baseline = _compute_baseline(test_rows, template_name, direction, cooldown)

            train_eval = evaluate_subset(
                train_rows, template_name, direction, conditions, train_baseline, min_train_trades, cooldown
            )
            test_eval = evaluate_subset(
                test_rows, template_name, direction, conditions, test_baseline, min_test_trades, cooldown
            )

            if not train_eval or not test_eval:
                if failed_reason is None:
                    failed_reason = "insufficient_sample"
                saw_failure = True
                window_results.append(
                    {
                        "train_months": window["train_months"],
                        "test_months": window["test_months"],
                        "train_stats": train_eval,
                        "test_stats": test_eval,
                        "passed": False,
                        "failure_reason": "insufficient_sample",
                    }
                )
                continue

            train_pass = (
                train_eval["edge_win_rate"] > 5.0
                and train_eval["avg_pnl_pct"] > 0
                and train_eval["p_value"] < validation_p
                and train_eval["profit_factor"] > 1.3
            )
            test_pass = (
                test_eval["edge_win_rate"] > 5.0
                and test_eval["avg_pnl_pct"] > 0
                and test_eval["profit_factor"] > 1.3
            )
            window_results.append(
                {
                    "train_months": window["train_months"],
                    "test_months": window["test_months"],
                    "train_stats": train_eval,
                    "test_stats": test_eval,
                    "passed": bool(train_pass and test_pass),
                }
            )
            if not (train_pass and test_pass):
                if failed_reason is None:
                    failed_reason = "failed_out_of_sample"
                saw_failure = True

        if window_results and not saw_failure and failed_reason is None:
            validated_setup = {
                "name": _setup_name(template_name, direction, conditions),
                "template": template_name,
                "direction": direction,
                "conditions": conditions,
                "train_stats": window_results[-1]["train_stats"],
                "test_stats": window_results[-1]["test_stats"],
                "tp_sl": dict(SETUP_TEMPLATES[template_name]),
                "by_symbol": _per_symbol_breakdown(rows, template_name, direction, conditions, cooldown),
                "window_results": window_results,
            }
            if scope_context:
                validated_setup.update(scope_context)
            validated.append(validated_setup)
        else:
            rejected_setup = {
                "name": _setup_name(template_name, direction, conditions),
                "template": template_name,
                "direction": direction,
                "conditions": conditions,
                "reason": failed_reason or "no_validation_windows",
            }
            if window_results:
                rejected_setup["window_results"] = window_results
            if scope_context:
                rejected_setup.update(scope_context)
            rejected.append(rejected_setup)
    return validated, rejected


def _metric_value(stats: dict, key: str) -> float:
    """Normalize metrics for comparison, including infinite profit factor."""
    value = stats.get(key, 0.0)
    if value == float("inf"):
        return 1_000_000.0
    return float(value)


def _setup_signature(setup: dict) -> tuple:
    """Create a compact signature for detecting effectively identical setups."""
    window_signature = []
    for window in setup.get("window_results", []):
        window_signature.append(
            (
                tuple(window["train_months"]),
                tuple(window["test_months"]),
                int(window["train_stats"]["count"]),
                round(_metric_value(window["train_stats"], "win_rate"), 4),
                round(_metric_value(window["train_stats"], "avg_pnl_pct"), 4),
                round(_metric_value(window["train_stats"], "profit_factor"), 4),
                int(window["test_stats"]["count"]),
                round(_metric_value(window["test_stats"], "win_rate"), 4),
                round(_metric_value(window["test_stats"], "avg_pnl_pct"), 4),
                round(_metric_value(window["test_stats"], "profit_factor"), 4),
            )
        )
    by_symbol_signature = tuple(
        (
            symbol,
            int(stats["trades"]),
            round(_metric_value(stats, "win_rate"), 4),
            round(_metric_value(stats, "avg_pnl_pct"), 4),
            round(_metric_value(stats, "profit_factor"), 4),
        )
        for symbol, stats in sorted(setup.get("by_symbol", {}).items())
    )
    return window_signature, by_symbol_signature


def _average_window_metric(setup: dict, split: str, metric: str) -> float:
    """Average one metric across validation windows, falling back to summary stats."""
    values = [
        _metric_value(window[f"{split}_stats"], metric)
        for window in setup.get("window_results", [])
        if f"{split}_stats" in window
    ]
    if values:
        return sum(values) / len(values)
    return _metric_value(setup.get(f"{split}_stats", {}), metric)


def _is_superset(candidate: dict, simpler_setup: dict) -> bool:
    """True when candidate is a strict condition superset of a simpler setup."""
    candidate_conditions = set(candidate["conditions"])
    simpler_conditions = set(simpler_setup["conditions"])
    return len(candidate_conditions) > len(simpler_conditions) and simpler_conditions.issubset(candidate_conditions)


def _has_material_increment(candidate: dict, simpler_setup: dict) -> bool:
    """Require a stricter setup to add meaningful train/test improvement."""
    deltas = {
        "train_avg_pnl_pct": _average_window_metric(candidate, "train", "avg_pnl_pct")
        - _average_window_metric(simpler_setup, "train", "avg_pnl_pct"),
        "test_avg_pnl_pct": _average_window_metric(candidate, "test", "avg_pnl_pct")
        - _average_window_metric(simpler_setup, "test", "avg_pnl_pct"),
        "train_profit_factor": _average_window_metric(candidate, "train", "profit_factor")
        - _average_window_metric(simpler_setup, "train", "profit_factor"),
        "test_profit_factor": _average_window_metric(candidate, "test", "profit_factor")
        - _average_window_metric(simpler_setup, "test", "profit_factor"),
        "test_edge_win_rate": _average_window_metric(candidate, "test", "edge_win_rate")
        - _average_window_metric(simpler_setup, "test", "edge_win_rate"),
    }
    return any(deltas[key] >= threshold for key, threshold in DOMINANCE_THRESHOLDS.items())


def dedupe_validated_setups(validated_setups: list[dict]) -> tuple[list[dict], list[dict]]:
    """Remove redundant validated setups while preserving meaningfully distinct ones."""
    normalized_setups = []
    for setup in validated_setups:
        normalized = dict(setup)
        normalized["conditions"] = normalize_conditions(setup["conditions"])
        normalized["template"] = _normalize_template_name(setup["template"])
        normalized["name"] = _setup_name(setup["template"], setup["direction"], normalized["conditions"])
        normalized_setups.append(normalized)

    normalized_setups.sort(
        key=lambda setup: (
            setup["template"],
            setup["direction"],
            len(setup["conditions"]),
            -_metric_value(setup["test_stats"], "profit_factor"),
            -_metric_value(setup["test_stats"], "avg_pnl_pct"),
            setup["name"],
        )
    )

    kept = []
    removed = []
    for candidate in normalized_setups:
        removal = None
        for simpler_setup in kept:
            if candidate["template"] != simpler_setup["template"] or candidate["direction"] != simpler_setup["direction"]:
                continue
            if set(candidate["conditions"]) == set(simpler_setup["conditions"]):
                removal = {
                    "name": candidate["name"],
                    "template": candidate["template"],
                    "direction": candidate["direction"],
                    "conditions": candidate["conditions"],
                    "reason": "duplicate_conditions",
                    "kept_setup": simpler_setup["name"],
                }
                break
            if not _is_superset(candidate, simpler_setup):
                continue
            if _setup_signature(candidate) == _setup_signature(simpler_setup):
                removal = {
                    "name": candidate["name"],
                    "template": candidate["template"],
                    "direction": candidate["direction"],
                    "conditions": candidate["conditions"],
                    "reason": "redundant_superset",
                    "kept_setup": simpler_setup["name"],
                }
                break
            if not _has_material_increment(candidate, simpler_setup):
                removal = {
                    "name": candidate["name"],
                    "template": candidate["template"],
                    "direction": candidate["direction"],
                    "conditions": candidate["conditions"],
                    "reason": "dominated_superset",
                    "kept_setup": simpler_setup["name"],
                }
                break
        if removal:
            removed.append(removal)
            continue
        kept.append(candidate)

    return kept, removed


def build_validated_setups_export(report: dict) -> dict:
    """Create the lightweight production-facing validated setup export."""
    validated_long = report["validated_setups"]["long"]
    validated_short = report["validated_setups"]["short"]
    dedupe_removed = report.get("dedupe_removed_setups", {"long": [], "short": []})

    return {
        "schema_version": 1,
        "metadata": {
            "generated_at": report["metadata"]["generated_at"],
            "data_range": report["metadata"]["data_range"],
            "symbols": report["metadata"]["symbols"],
            "train_months": report["metadata"]["train_months"],
            "test_months": report["metadata"]["test_months"],
            "step_months": report["metadata"]["step_months"],
            "eval_cooldown_candles": report["metadata"]["eval_cooldown_candles"],
            "fee_pct": report["metadata"]["fee_pct"],
            "slippage_pct": report["metadata"]["slippage_pct"],
            "dedupe_policy": {
                "drop_strict_superset_if_signature_matches": True,
                "dominance_thresholds": dict(DOMINANCE_THRESHOLDS),
                "removed_counts": {
                    "long": len(dedupe_removed.get("long", [])),
                    "short": len(dedupe_removed.get("short", [])),
                },
            },
        },
        "validated_setups": {
            "long": [_export_runtime_setup(setup) for setup in validated_long],
            "short": [_export_runtime_setup(setup) for setup in validated_short],
        },
    }


def analyze_rows_scope(
    rows: list[dict],
    template_names: list[str],
    combo_max_size: int,
    max_combo_tests: int,
    min_train_trades: int,
    min_test_trades: int,
    prefilter_p: float,
    validation_p: float,
    train_months: int,
    test_months: int,
    step_months: int,
    scope_context: Optional[dict] = None,
) -> dict:
    """Run the full setup-discovery pipeline on one research scope."""
    scope_context = dict(scope_context or {})
    report = {
        "scope": scope_context,
        "summary": {
            "rows_analyzed": len(rows),
            "symbols_in_scope": sorted({row["symbol"] for row in rows}),
            "templates_in_scope": list(template_names),
        },
        "analysis": {},
        "validated_setups": {"long": [], "short": []},
        "raw_validated_setups": {"long": [], "short": []},
        "dedupe_removed_setups": {"long": [], "short": []},
        "rejected_setups": [],
    }

    for template_name in template_names:
        for direction in ("LONG", "SHORT"):
            analysis_key = f"{template_name}_{direction.lower()}"
            analysis_result, combo_results = analyze_conditions(
                rows=rows,
                template_name=template_name,
                direction=direction,
                min_trades=min_train_trades,
                combo_max_size=combo_max_size,
                max_combo_tests=max_combo_tests,
                prefilter_p=prefilter_p,
            )
            report["analysis"][analysis_key] = analysis_result
            promising_individuals = [
                result
                for result in analysis_result["individual_conditions"]
                if result["p_value"] < prefilter_p and result["avg_pnl_pct"] > 0
            ]
            validated, rejected = validate_candidates(
                rows=rows,
                template_name=template_name,
                direction=direction,
                candidate_results=promising_individuals + combo_results,
                train_months=train_months,
                test_months=test_months,
                step_months=step_months,
                min_train_trades=min_train_trades,
                min_test_trades=min_test_trades,
                validation_p=validation_p,
                cooldown=EVAL_COOLDOWN_CANDLES,
                scope_context=scope_context,
            )
            bucket = "long" if direction == "LONG" else "short"
            report["raw_validated_setups"][bucket].extend(validated)
            report["rejected_setups"].extend(rejected)

    for bucket in ("long", "short"):
        deduped, removed = dedupe_validated_setups(report["raw_validated_setups"][bucket])
        report["validated_setups"][bucket] = deduped
        report["dedupe_removed_setups"][bucket] = removed

    report["summary"].update(
        {
            "validated_long_raw": len(report["raw_validated_setups"]["long"]),
            "validated_short_raw": len(report["raw_validated_setups"]["short"]),
            "validated_long": len(report["validated_setups"]["long"]),
            "validated_short": len(report["validated_setups"]["short"]),
            "rejected": len(report["rejected_setups"]),
        }
    )
    return report


def analyze_symbol_universe(
    symbols: list[str],
    months: int,
    warmup_candles: int,
    max_holding_candles: int,
    combo_max_size: int,
    max_combo_tests: int,
    min_train_trades: int,
    min_test_trades: int,
    fee_pct: float,
    slippage_pct: float,
    train_months: int,
    test_months: int,
    step_months: int,
    prefilter_p: float,
    validation_p: float,
    end_dt: Optional[datetime] = None,
    workers: int = 1,
    discovery_variants: Optional[list[str]] = None,
) -> dict:
    """Run the full analysis workflow and return the JSON-serializable report."""
    normalized_variants = _normalize_discovery_variants(discovery_variants)
    end_dt = end_dt.astimezone(timezone.utc) if end_dt is not None else datetime.now(timezone.utc)
    warmup_days = 170
    analysis_start_dt = end_dt - timedelta(days=30 * months)
    fetch_start_dt = analysis_start_dt - timedelta(days=warmup_days)
    start_ms = int(fetch_start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    analysis_start_ms = int(analysis_start_dt.timestamp() * 1000)

    all_rows = []
    worker_count = max(1, workers)
    if worker_count == 1 or len(symbols) == 1:
        for symbol in symbols:
            log.info(f"Loading {symbol} candles for statistical analysis...")
            loaded_symbol, rows = load_and_build_symbol_rows(
                symbol=symbol,
                start_ms=start_ms,
                end_ms=end_ms,
                analysis_start_ms=analysis_start_ms,
                warmup_candles=warmup_candles,
                max_holding_candles=max_holding_candles,
                fee_pct=fee_pct,
                slippage_pct=slippage_pct,
            )
            log.info(f"  {loaded_symbol}: built {len(rows)} study rows")
            all_rows.extend(rows)
    else:
        log.info("Building study rows with %d workers...", worker_count)
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(
                    load_and_build_symbol_rows,
                    symbol,
                    start_ms,
                    end_ms,
                    analysis_start_ms,
                    warmup_candles,
                    max_holding_candles,
                    fee_pct,
                    slippage_pct,
                ): symbol
                for symbol in symbols
            }
            for future in as_completed(future_map):
                loaded_symbol, rows = future.result()
                log.info(f"  {loaded_symbol}: built {len(rows)} study rows")
                all_rows.extend(rows)

    report = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_range": f"{analysis_start_dt.strftime('%Y-%m')} to {end_dt.strftime('%Y-%m')}",
            "symbols": symbols,
            "rows_analyzed": len(all_rows),
            "conditions_tested": len(ALL_CONDITIONS),
            "warmup_candles": warmup_candles,
            "max_holding_candles": max_holding_candles,
            "eval_cooldown_candles": EVAL_COOLDOWN_CANDLES,
            "fee_pct": fee_pct,
            "slippage_pct": slippage_pct,
            "combo_max_size": combo_max_size,
            "max_combo_tests": max_combo_tests,
            "train_months": train_months,
            "test_months": test_months,
            "step_months": step_months,
            "workers": worker_count,
            "discovery_variants": normalized_variants,
        },
        "analysis": {},
        "validated_setups": {"long": [], "short": []},
        "raw_validated_setups": {"long": [], "short": []},
        "dedupe_removed_setups": {"long": [], "short": []},
        "rejected_setups": [],
        "summary": {},
    }

    pooled_report = analyze_rows_scope(
        rows=all_rows,
        template_names=sorted(SETUP_TEMPLATES),
        combo_max_size=combo_max_size,
        max_combo_tests=max_combo_tests,
        min_train_trades=min_train_trades,
        min_test_trades=min_test_trades,
        prefilter_p=prefilter_p,
        validation_p=validation_p,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
        scope_context={"scope_key": "pooled", "scope_type": "pooled"},
    )
    report["analysis"] = pooled_report["analysis"]
    report["validated_setups"] = pooled_report["validated_setups"]
    report["raw_validated_setups"] = pooled_report["raw_validated_setups"]
    report["dedupe_removed_setups"] = pooled_report["dedupe_removed_setups"]
    report["rejected_setups"] = pooled_report["rejected_setups"]
    report["summary"] = pooled_report["summary"]

    variant_views = build_analysis_views(all_rows, symbols, normalized_variants)
    extra_variant_reports = {}
    for view_key, view in variant_views.items():
        if view_key == "pooled":
            continue
        extra_variant_reports[view_key] = analyze_rows_scope(
            rows=view["rows"],
            template_names=view["template_names"],
            combo_max_size=combo_max_size,
            max_combo_tests=max_combo_tests,
            min_train_trades=min_train_trades,
            min_test_trades=min_test_trades,
            prefilter_p=prefilter_p,
            validation_p=validation_p,
            train_months=train_months,
            test_months=test_months,
            step_months=step_months,
            scope_context=view["scope"],
        )
    if extra_variant_reports:
        report["discovery_variants"] = extra_variant_reports

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Statistical setup analyzer for the trading bot")
    parser.add_argument("--months", type=int, default=12, help="Months of data to analyze")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="Symbols to analyze")
    parser.add_argument("--warmup-candles", type=int, default=DEFAULT_WARMUP_CANDLES, help="Warmup candles before analysis")
    parser.add_argument("--max-holding-candles", type=int, default=DEFAULT_MAX_HOLDING_CANDLES, help="Max holding period in candles")
    parser.add_argument("--combo-max-size", type=int, default=3, help="Largest condition combo size to test")
    parser.add_argument("--max-combo-tests", type=int, default=MAX_COMBO_TESTS_DEFAULT, help="Maximum combo tests per direction/template")
    parser.add_argument("--min-train-trades", type=int, default=DEFAULT_MIN_TRAIN_TRADES, help="Minimum train-sample trades")
    parser.add_argument("--min-test-trades", type=int, default=DEFAULT_MIN_TEST_TRADES, help="Minimum test-sample trades")
    parser.add_argument("--train-months", type=int, default=6, help="Walk-forward training months")
    parser.add_argument("--test-months", type=int, default=3, help="Walk-forward test months")
    parser.add_argument("--step-months", type=int, default=3, help="Walk-forward shift size")
    parser.add_argument("--prefilter-p", type=float, default=DEFAULT_PREFILTER_P, help="P-value threshold for combo prefiltering")
    parser.add_argument("--validation-p", type=float, default=DEFAULT_VALIDATION_P, help="P-value threshold for validation")
    parser.add_argument("--fee-pct", type=float, default=DEFAULT_FEE_PCT, help="Round-trip fee percent")
    parser.add_argument("--slippage-pct", type=float, default=DEFAULT_SLIPPAGE_PCT, help="Adverse slippage percent per side")
    parser.add_argument(
        "--discovery-variants",
        nargs="+",
        choices=VALID_DISCOVERY_VARIANTS,
        default=list(DEFAULT_DISCOVERY_VARIANTS),
        help="Additional research views to run: pooled, symbol_specific",
    )
    parser.add_argument("--end-date", type=str, default=None, help="Optional UTC end date (YYYY-MM-DD) for reproducible cached analysis")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1), help="Parallel workers for per-symbol row building")
    parser.add_argument("--output", type=str, default=DEFAULT_REPORT_OUTPUT, help="Full analysis report JSON path")
    parser.add_argument(
        "--validated-output",
        type=str,
        default=DEFAULT_VALIDATED_OUTPUT,
        help="Deduped validated setups JSON export path",
    )
    args = parser.parse_args()

    end_dt = None
    if args.end_date:
        end_dt = datetime.strptime(args.end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    report = analyze_symbol_universe(
        symbols=args.symbols,
        months=args.months,
        warmup_candles=args.warmup_candles,
        max_holding_candles=args.max_holding_candles,
        combo_max_size=args.combo_max_size,
        max_combo_tests=args.max_combo_tests,
        min_train_trades=args.min_train_trades,
        min_test_trades=args.min_test_trades,
        fee_pct=args.fee_pct,
        slippage_pct=args.slippage_pct,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        prefilter_p=args.prefilter_p,
        validation_p=args.validation_p,
        end_dt=end_dt,
        workers=args.workers,
        discovery_variants=args.discovery_variants,
    )

    validated_export = build_validated_setups_export(report)

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    with open(args.validated_output, "w", encoding="utf-8") as handle:
        json.dump(validated_export, handle, indent=2)

    log.info(
        "Saved analysis to %s",
        args.output,
    )
    log.info(
        "Saved validated setups to %s (%d long validated, %d short validated)",
        args.validated_output,
        len(report["validated_setups"]["long"]),
        len(report["validated_setups"]["short"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
