# Score Band Analysis Feature

## Overview

The score band analysis breaks down backtest performance by signal confidence levels, revealing which score ranges perform best. This provides quantifiable insight for threshold optimization.

## Feature

### What It Does

Divides all trades into confidence bands based on their entry signal score:

```
Score Band   | Trades | Wins | Win Rate | P&L      | Avg Win | Avg Loss
5.0-5.5      |   10   |   3  |  30.0%   | +$25.50  | +2.1%   | -1.8%
5.5-6.0      |   15   |   5  |  33.3%   | +$18.75  | +1.9%   | -1.5%
6.0-6.5      |   20   |   8  |  40.0%   | +$52.00  | +3.2%   | -1.6%
6.5-7.0      |   25   |  10  |  40.0%   | +$75.00  | +3.8%   | -2.0%
7.0-8.0      |   30   |  12  |  40.0%   | +$108.0  | +4.2%   | -2.1%
```

### Output Location

Results are included in `backtest-results.json`:

```json
{
  "by_score_band": {
    "6.0-6.5": {
      "trades": 20,
      "wins": 8,
      "losses": 12,
      "win_rate": 40.0,
      "pnl_usd": 52.00,
      "avg_win_pct": 3.20,
      "avg_loss_pct": -1.60,
      "avg_pnl_pct": 2.60
    },
    ...
  }
}
```

## How to Use

### 1. Run a Backtest
```bash
PYTHONPATH=src python3 -m trading_bot.backtester --months 6
```

### 2. Extract Score Band Data
```python
import json

with open('src/trading_bot/backtest-results.json') as f:
    data = json.load(f)

score_bands = data['by_score_band']

# Find best-performing band
best_band = max(score_bands.items(), key=lambda x: x[1]['win_rate'])
print(f"Best band by win rate: {best_band[0]} at {best_band[1]['win_rate']}% WR")

# Find most profitable band
most_profitable = max(score_bands.items(), key=lambda x: x[1]['pnl_usd'])
print(f"Most profitable: {most_profitable[0]} with ${most_profitable[1]['pnl_usd']:.2f}")
```

### 3. Interpret Results

**Key Insights:**
- **Win Rate by Band**: Shows which score ranges generate cleaner, more reliable signals
- **P&L by Band**: Reveals total profitability across confidence levels
- **Avg Win/Loss**: Indicates risk/reward ratio per band

## Example Analysis

From 1-month sample:

| Band    | Trades | WR    | P&L      | Best For? |
|---------|--------|-------|----------|-----------|
| 6.0-6.5 | 7      | 57.1% | +$22.54  | Low confidence signals |
| 6.5-7.0 | 4      | 75.0% | +$36.06  | **Sweet spot** |
| 7.0-8.0 | 10     | 70.0% | +$61.57  | High confidence signals |

**Finding**: 6.5-7.0 band has highest win rate (75%), but 7.0-8.0 captures most volume (10 trades, +$61.57).

## Use Cases

1. **Threshold Optimization**: Identify which score range (5.0, 5.5, 6.0, 6.5, 7.0+) performs best
2. **Risk Management**: Adjust position sizing based on signal strength
3. **Score Ladder Entry**: Use different position sizes per band
4. **Regime Detection**: See if certain regimes favor higher/lower confidence signals

## Testing

Comprehensive test suite in `tests/test_backtester_scoreband.py`:

- ✅ 11 unit tests covering all score band calculations
- ✅ Edge case handling (empty bands, zero trades, no division by zero)
- ✅ Boundary condition validation
- ✅ Integration tests

Run tests:
```bash
python3 -m pytest tests/test_backtester_scoreband.py -v
```

All 44 tests pass (including 11 new score band tests + 33 existing tests).

## Implementation Details

- **Location**: `src/trading_bot/backtester.py` (lines 820-844)
- **Score Bands**: [5.0-5.5), [5.5-6.0), [6.0-6.5), [6.5-7.0), [7.0-8.0)
- **Metrics**: Trades, Wins/Losses, Win Rate, P&L (USD), Avg Win%, Avg Loss%, Avg P&L%
- **Empty Bands**: Excluded from output (only bands with trades included)

---

**Next Steps**: Use score band data to guide threshold optimization decisions.
