"""Unit tests for core.tp_sl.compute_tp_sl."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_bot.core.tp_sl import compute_tp_sl


# ── LONG ─────────────────────────────────────────────────────────────────────

def test_long_standard_template():
    # entry=100, ATR=2, sl_mult=1.5, rr=2.0
    # sl_dist = 3, tp_dist = 6
    tp, sl = compute_tp_sl("LONG", 100.0, 2.0, 1.5, 2.0)
    assert tp == 106.0
    assert sl == 97.0


def test_long_wide_template():
    # entry=100, ATR=2, sl_mult=2.0, rr=2.5
    # sl_dist = 4, tp_dist = 10
    tp, sl = compute_tp_sl("LONG", 100.0, 2.0, 2.0, 2.5)
    assert tp == 110.0
    assert sl == 96.0


# ── SHORT ────────────────────────────────────────────────────────────────────

def test_short_standard_template():
    # entry=100, ATR=2, sl_mult=1.5, rr=2.0 → tp_dist=6, sl_dist=3
    tp, sl = compute_tp_sl("SHORT", 100.0, 2.0, 1.5, 2.0)
    assert tp == 94.0
    assert sl == 103.0


def test_short_wide_template():
    tp, sl = compute_tp_sl("SHORT", 100.0, 2.0, 2.0, 2.5)
    assert tp == 90.0
    assert sl == 104.0


# ── Distances preserved when entry shifts (the "anchor" property) ───────────

def test_long_distances_preserved_across_entries():
    # Same recipe at different entries → same distances
    tp1, sl1 = compute_tp_sl("LONG", 100.0, 2.0, 1.5, 2.0)
    tp2, sl2 = compute_tp_sl("LONG", 103.0, 2.0, 1.5, 2.0)
    assert (tp1 - 100.0) == (tp2 - 103.0)   # TP distance preserved
    assert (100.0 - sl1) == (103.0 - sl2)   # SL distance preserved


def test_short_distances_preserved_across_entries():
    tp1, sl1 = compute_tp_sl("SHORT", 100.0, 2.0, 1.5, 2.0)
    tp2, sl2 = compute_tp_sl("SHORT", 97.0, 2.0, 1.5, 2.0)
    assert (100.0 - tp1) == (97.0 - tp2)
    assert (sl1 - 100.0) == (sl2 - 97.0)


# ── Edge: zero ATR returns entry on both sides ───────────────────────────────

def test_zero_atr_collapses_to_entry():
    tp, sl = compute_tp_sl("LONG", 100.0, 0.0, 1.5, 2.0)
    assert tp == 100.0
    assert sl == 100.0
