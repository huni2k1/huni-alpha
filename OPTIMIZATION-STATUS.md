# Optimization V1 Status (ARCHIVED - Decision Made 2026-03-28)

## Current State

**Status:** ✅ COMPLETED — 6.0 threshold confirmed as default
**Decision:** Merged to main (commit 0e625a2 - Mar 27)
**Objective:** Optimize breakout threshold from 6.0 to improve returns — DONE

---

## Test Results Summary

### 6-Month Performance (Sep 2025 - Mar 2026)
| Threshold | Return | Trades | WR | Max DD |
|-----------|--------|--------|-----|---------|
| 6.0 (baseline) | +6.3% | 131 | 35.9% | 10.5% |
| 7.0 | **+16.1%** | 61 | 39.3% | 9.1% |

### 12-Month Performance (Apr 2025 - Mar 2026)
| Threshold | Return | Trades | WR | Max DD | Status |
|-----------|--------|--------|-----|---------|--------|
| 5.0 | -24.6% | 153 | 23.5% | 25.1% | 🔥 Unusable |
| **6.0** | **+8.0%** | 263 | 33.1% | 16.9% | ✅ Optimal |
| 7.0 | +4.2% | 120 | 33.3% | 13.1% | Recent only |

---

## Key Findings

1. **6.0 is the robust default** — +8% annual, healthy win rate, controlled drawdown
2. **7.0 optimizes for bull markets** — +16.1% on recent 6 months, but regresses to +4.2% on 12 months
3. **5.0 is a trap** — -24.6% annual loss, triggers circuit breaker, 23.5% win rate
4. **Breakout vs Trend split at 6.0:**
   - Breakout: +$17.33 (now profitable!)
   - Trend: +$62.42 (strong)

---

## Recommendation

**✅ Keep 6.0 as default** — Provides steady +8% returns with robust signal quality across full market cycles.

**Why not 7.0?**
- Mixed results: excellent 6-month, poor 12-month
- Relies entirely on Trend strategy (breakout becomes negative)
- Risk: If market regime changes, returns collapse

**Next Phase:**
- Explore signal quality improvements (Bollinger width, stricter ADX, volume confirmation)
- Test symbol-specific thresholds (major caps vs alts)
- Target: +12-15% 6-month returns without increasing drawdown

---

## Documentation Updated

- ✅ `README.md` — Updated performance table and threshold recommendations
- ✅ `docs/ARCHITECTURE.md` — Updated current performance metrics
- ✅ `docs/OPTIMIZATION-V1-BREAKOUT-THRESHOLD.md` — Detailed analysis (NEW)
- ✅ `OPTIMIZATION-STATUS.md` — This file (NEW)

---

## Merge Decision — IMPLEMENTED

✅ **Decision: Keep 6.0 default** — Confirmed and merged to main
- 12-month backtest (Mar 28): +43.45% return (not just +8% as originally estimated)
- 6-month rolling: +14.56%
- Robust across market regimes
- 392 trades, 39.3% WR, 1.26 PF

**Recent bug fix (Mar 28):** Asia session filter now uses candle timestamps (not current time), revealing true strategy performance. Earlier tests underestimated returns.

**Branch status:** All feature branches cleaned up. Main codebase streamlined.

---

*Archived: 2026-03-28 (decision finalized)*
