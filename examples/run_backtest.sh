#!/bin/bash
# Run a 12-month backtest with optimized parameters

cd "$(dirname "$0")/.."

echo "Running 12-month backtest with threshold 6.5..."
python3 -m trading_bot.backtester \
  --months 12 \
  --entry-threshold 6.5 \
  --fee-pct 0.14 \
  --slippage-pct 0.02 \
  --max-drawdown 35

echo "Results saved to data/backtest-results.json"
