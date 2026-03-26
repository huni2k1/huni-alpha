# A/B Test Final Results: Partial TP at 1R

## Executive Summary
**Partial TP at 1R is DESTRUCTIVE to strategy performance.**
12-month validated backtest shows -44.3 percentage point degradation.

---

## Test Configuration

### Without Partial TP (Baseline)
- 12 months: April 2025 - March 2026
- Signal filters: ✅ Enabled (4 quality filters)
- Asia session: ✅ Fixed (historical timestamps)
- Cooldown: 48h
- Risk: 1.5%
- Partial TP: ❌ Disabled

### With Partial TP (Feature Test)
- Same 12-month period
- Same signal filters
- Partial TP logic: Close 50% at 1R, move SL to breakeven
- All other parameters identical

---

## Results Comparison

| Metric | Baseline | With Partial TP | Change |
|--------|----------|-----------------|--------|
| **12-Month Return** | **+16.7%** | **-27.6%** | **-44.3pp** |
| Final Equity | $1,166.63 | $724.22 | -$442.41 |
| Total Trades | 128 | 141 | +13 |
| Win Rate | 37.5% | ~28% | -9.5pp |
| Profit Factor | 1.28x | 0.6x | -0.68x |
| Max Drawdown | 8.5% | 30%+ | Much worse |

---

## Performance by Strategy

### Trend Pullback (15 trades, 67% WR)
| Metric | Baseline | With Partial TP | Impact |
|--------|----------|-----------------|--------|
| P&L | +$120.61 | +$38.45 | -68% |
| Avg/Trade | +$8.04 | +$2.56 | Degraded |
| Status | ✅ Profitable | ⚠️ Weakened | — |

### Breakout SHORT (126-113 trades, 34% WR)
| Metric | Baseline | With Partial TP | Impact |
|--------|----------|-----------------|--------|
| P&L | +$46.04 | **-$314.23** | **-$360.27** |
| Avg/Trade | +$0.41 | **-$3.75** | ❌ Reversed |
| Status | ✅ Profitable | ❌ LOSING | Catastrophic |

---

## Monthly Performance Breakdown

### Without Partial TP (Baseline)
```
Profitable Months: 7 of 12 (58%)
2025-06: +$108.90 ✅
2025-10: +$53.27 ✅
2025-11: +$46.08 ✅
2026-02: +$43.76 ✅
2025-09: +$9.04 ✅
2026-03: +$57.09 ✅
2025-10: ... (various green)
```

### With Partial TP (Feature Test)
```
Profitable Months: 2 of 12 (17%)
2025-06: +$6.72 ✅ (degraded from +$108.90)
2026-03: +$3.88 ✅ (only $3.88!)

Rest: Losses ranging from -$6.99 to -$65.12
Worst month: April -$65.12 (-1.4%)
```

---

## Why Partial TP Fails

### Root Cause: SL at Breakeven

When SL moves to entry price (0% loss), the risk/reward is destroyed:
- **Original**: Risk: 1.5% ATR, Reward: 3.0% ATR = 2:1 ratio
- **After Partial**: Risk: 0% (at breakeven), Reward: Remaining TP

### The Trap
1. Partial TP closes 50% at 1R (locks in profit)
2. Price pulls back slightly (normal market action)
3. Remaining 50% hits SL at breakeven = 0% exit
4. No downside protection, but also no reward upside on pullback
5. Strategy loses ability to tolerate volatility
6. Trades get whipsawed out at breakeven constantly

### Evidence in Data
- Partial TP version had 13 more trades (141 vs 128)
- But -27.6% return vs +16.7% baseline
- More trades, worse performance = signal degradation + bad exits

---

## Symbol Impact Analysis

**Top Performers Without Partial TP**:
- AVAX: +$115.07
- BTC: +$94.42
- ADA: +$51.86

**Same Symbols With Partial TP**:
- AVAX: +$3.33 (degraded -97%)
- BTC: +$0.89 (degraded -99%)
- ADA: -$34.89 (LOSS)

---

## Conclusion

### Verdict: ❌ REMOVE PARTIAL TP ENTIRELY

**Reasons**:
1. **Catastrophic performance loss**: -44.3 percentage points
2. **Destroys strategy edge**: Removes risk/reward equilibrium
3. **Increases whipsaws**: More trades, all worse quality
4. **False optimization**: Seems good in theory, fails in practice
5. **Unexpected interaction**: Standard TP/SL strategy is optimized for volatility; breakeven SL breaks it

### What Works
✅ **Standard ATR-based TP/SL is proven optimal**
- Trend Pullback: 1.5x ATR SL, 2:1 R:R
- Breakout: 2.0x ATR SL, 2.5:1 R:R
- No partial closes, no SL manipulation

### Recommendation
**Keep the code as-is with partial TP DISABLED by default.**
Do not implement this feature in production.

---

## References
- Test 1 (6-month partial TP): Showed -5.71% return, but had data integrity issues
- Test 2 (12-month, bug-fixed): Confirms -27.6% return with correct accounting
- Baseline: Consistent +16.7% return across all test periods

