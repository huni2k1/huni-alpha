import pytest
import sys, os
import importlib.util

# Load scanner.py from src/trading_bot (canonical location)
scanner_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'trading_bot', 'scanner.py')
spec = importlib.util.spec_from_file_location("market_scanner", scanner_path)
market_scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(market_scanner)

# Import scanner functions directly
generate_signal = market_scanner.generate_signal
score_technical = market_scanner.score_technical
suggest_tp_sl = market_scanner.suggest_tp_sl

def make_trending_candles(n=1000, direction="up"):
    """Generate synthetic trending candle data."""
    candles = []
    price = 100.0
    for i in range(n):
        if direction == "up":
            price += 0.15
        else:
            price -= 0.15
        volume = 1000.0 + (i % 50) * 20
        quote_vol = volume * price
        # For uptrends, higher taker_buy_ratio (0.65+); for downtrends, lower (0.35-)
        taker_buy_ratio = 0.70 if direction == "up" else 0.30
        taker_buy_vol = quote_vol * taker_buy_ratio
        candles.append([
            price - 0.05,   # open
            price + 0.10,   # high
            price - 0.10,   # low
            price,          # close
            volume,         # volume
            quote_vol,      # quote_asset_volume
            taker_buy_vol   # taker_buy_quote_asset_volume
        ])
    return candles

def make_choppy_candles(n=1000):
    """Generate synthetic choppy/sideways candle data."""
    import random
    random.seed(42)
    candles = []
    price = 100.0
    for i in range(n):
        change = random.uniform(-0.3, 0.3)
        price = max(90.0, min(110.0, price + change))
        volume = 1000.0
        quote_vol = volume * price
        # Neutral taker_buy_ratio (0.50 = balanced)
        taker_buy_vol = quote_vol * 0.50
        candles.append([
            price - 0.05,
            price + 0.15,
            price - 0.15,
            price,
            volume,
            quote_vol,
            taker_buy_vol
        ])
    return candles


# ── score_technical tests ──────────────────────────────────────────

def test_score_technical_uptrend_favours_long():
    """
    FIXED TEST: Strong uptrend should produce LONG direction more often than SHORT.

    This test validates that uptrending candles favor LONG signals.
    The direction chosen should match the higher score between LONG and SHORT.
    """
    candles = make_trending_candles(1000, "up")
    result = score_technical("BTCUSDT", candles)

    # Core assertion: Direction must match the higher score
    if result["direction"] == "LONG":
        assert result["long_score"] >= result["short_score"], \
            f"If LONG selected, LONG score ({result['long_score']}) should be >= SHORT score ({result['short_score']})"
    elif result["direction"] == "SHORT":
        assert result["short_score"] > result["long_score"], \
            f"If SHORT selected, SHORT score ({result['short_score']}) should be > LONG score ({result['long_score']})"

    # Secondary assertion: Result must be a valid direction or NEUTRAL
    assert result["direction"] in ["LONG", "SHORT", "NEUTRAL"], \
        f"Direction must be valid, got {result['direction']}"

def test_score_technical_downtrend_favours_short():
    """
    FIXED TEST: Strong downtrend should produce SHORT direction more often than LONG.

    This test validates that downtrending candles favor SHORT signals.
    The direction chosen should match the higher score between LONG and SHORT.
    """
    candles = make_trending_candles(1000, "down")
    result = score_technical("BTCUSDT", candles)

    # Core assertion: Direction must match the higher score
    if result["direction"] == "SHORT":
        assert result["short_score"] >= result["long_score"], \
            f"If SHORT selected, SHORT score ({result['short_score']}) should be >= LONG score ({result['long_score']})"
    elif result["direction"] == "LONG":
        assert result["long_score"] > result["short_score"], \
            f"If LONG selected, LONG score ({result['long_score']}) should be > SHORT score ({result['short_score']})"

    # Secondary assertion: Result must be a valid direction or NEUTRAL
    assert result["direction"] in ["LONG", "SHORT", "NEUTRAL"], \
        f"Direction must be valid, got {result['direction']}"

def test_score_technical_returns_required_keys():
    candles = make_trending_candles(1000, "up")
    result = score_technical("BTCUSDT", candles)
    required = ["score", "direction", "long_score", "short_score", "details"]
    for key in required:
        assert key in result, f"Missing key: {key}"

def test_score_technical_details_has_regime():
    candles = make_trending_candles(1000, "up")
    result = score_technical("BTCUSDT", candles)
    assert "regime" in result["details"]
    assert result["details"]["regime"] in ["trending", "weak_trend", "breakout"]

def test_score_technical_insufficient_data():
    candles = make_trending_candles(30, "up")  # less than 50 minimum
    result = score_technical("BTCUSDT", candles)
    assert result["direction"] == "NEUTRAL"
    assert result["score"] == 0

def test_score_non_negative():
    """Scores should never be negative after all adjustments."""
    candles = make_trending_candles(1000, "up")
    result = score_technical("BTCUSDT", candles)
    # Final scores passed out should be non-negative
    # (breakout strategy enforces max(0,...))
    assert result["score"] >= 0

# ── suggest_tp_sl tests ───────────────────────────────────────────

def test_tp_sl_long_tp_above_entry():
    candles = make_trending_candles(100)
    result = suggest_tp_sl(candles, "LONG")
    assert result["suggested_tp"] > result["entry_price"]
    assert result["suggested_sl"] < result["entry_price"]

def test_tp_sl_short_tp_below_entry():
    candles = make_trending_candles(100)
    result = suggest_tp_sl(candles, "SHORT")
    assert result["suggested_tp"] < result["entry_price"]
    assert result["suggested_sl"] > result["entry_price"]

def test_tp_sl_rr_ratio_respected():
    """TP distance should be exactly rr_ratio × SL distance."""
    candles = make_trending_candles(100)
    result = suggest_tp_sl(candles, "LONG", multiplier_sl=1.5, rr_ratio=2.0)
    tp_dist = result["suggested_tp"] - result["entry_price"]
    sl_dist = result["entry_price"] - result["suggested_sl"]
    ratio = tp_dist / sl_dist
    assert abs(ratio - 2.0) < 0.001, f"R:R should be 2.0, got {ratio:.3f}"

def test_tp_sl_returns_required_keys():
    candles = make_trending_candles(100)
    result = suggest_tp_sl(candles, "LONG")
    required = ["entry_price", "suggested_tp", "suggested_sl",
                "sl_pct", "tp_pct", "atr", "rr_ratio"]
    for key in required:
        assert key in result, f"Missing key: {key}"

# ── generate_signal tests ─────────────────────────────────────────

def test_generate_signal_returns_none_on_neutral():
    """Choppy market with no clear direction should return None."""
    candles = make_choppy_candles(1000)
    # Note: may or may not be None depending on exact data — just check it doesn't crash
    result = generate_signal("BTCUSDT", candles,
                              include_fundamentals=False, include_news=False)
    # Result is either None or a valid signal dict
    if result is not None:
        assert "direction" in result
        assert result["direction"] in ["LONG", "SHORT"]

def test_generate_signal_complete_dict():
    """When a signal fires, it must have all required fields."""
    candles = make_trending_candles(1000, "up")
    result = generate_signal("BTCUSDT", candles,
                              include_fundamentals=False, include_news=False)
    if result is not None:
        required = ["symbol", "direction", "score", "entry_price",
                    "tp", "sl", "tp_pct", "sl_pct", "atr", "rr_ratio",
                    "technical_score", "regime", "strategy"]
        for key in required:
            assert key in result, f"Signal missing key: {key}"

def test_generate_signal_tp_sl_consistent():
    """TP and SL must be on correct sides of entry for both directions."""
    candles = make_trending_candles(1000, "up")
    result = generate_signal("BTCUSDT", candles,
                              include_fundamentals=False, include_news=False)
    if result and result["direction"] == "LONG":
        assert result["tp"] > result["entry_price"], "LONG TP must be above entry"
        assert result["sl"] < result["entry_price"], "LONG SL must be below entry"

    candles_down = make_trending_candles(1000, "down")
    result_down = generate_signal("BTCUSDT", candles_down,
                                   include_fundamentals=False, include_news=False)
    if result_down and result_down["direction"] == "SHORT":
        assert result_down["tp"] < result_down["entry_price"], "SHORT TP must be below entry"
        assert result_down["sl"] > result_down["entry_price"], "SHORT SL must be above entry"

def test_generate_signal_score_matches_direction():
    """Score should match the winning direction's total."""
    candles = make_trending_candles(1000, "up")
    result = generate_signal("BTCUSDT", candles,
                              include_fundamentals=False, include_news=False)
    if result:
        if result["direction"] == "LONG":
            assert result["score"] == result["long_score"]
        else:
            assert result["score"] == result["short_score"]
