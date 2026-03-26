# Trading Bot Architecture

## Overview

Multi-regime technical analysis trading strategy with:
- **Real-time signal generation** (scanner)
- **Walk-forward backtesting** (backtester)
- **Comprehensive test suite** (33 tests)

## Directory Structure

```
trading-bot/
├── src/trading_bot/          # Main source code
│   ├── backtester.py         # Historical walk-forward testing
│   ├── scanner.py            # Live signal generation
│   ├── check_signal.py       # Signal validation utility
│   └── __init__.py
├── tests/                     # Test suite
│   ├── test_indicators.py    # Unit tests (19 tests)
│   ├── test_signals.py       # Integration tests (14 tests)
│   ├── conftest.py           # Pytest fixtures
│   └── README.md             # Test documentation
├── config/                    # Configuration files
│   ├── binance-real.json     # Binance API credentials
│   └── telegram.json         # Telegram alert credentials
├── data/                      # Backtest results & analysis
│   └── backtest-results.json # Latest backtest output
├── docs/                      # Documentation
│   └── ARCHITECTURE.md       # This file
├── examples/                  # Example scripts
│   └── run_backtest.sh       # Quick start backtest
├── README.md                  # Main documentation
├── LICENSE                    # MIT License
├── requirements.txt           # Python dependencies
└── .gitignore                # Git ignore rules
```

## Core Components

### Scanner (src/trading_bot/scanner.py)

Generates real-time trading signals using multi-regime technical analysis:

**Input:** Current candle window + market data
**Output:** Signal with score, direction, TP/SL, ATR

**Key Functions:**
- `score_technical()` — Technical analysis scoring (0-15 pts)
- `suggest_tp_sl()` — ATR-based take profit / stop loss
- `generate_signal()` — Complete signal generation

**Scoring System:**
- Technical: 0-7.5 pts (RSI, MACD, ADX, EMA, Volume)
- Fundamental: 0-5 pts (Fear & Greed, Funding rates, L/S ratio)
- News Sentiment: 0-3 pts (Headlines from 3 sources)

### Backtester (src/trading_bot/backtester.py)

Historical walk-forward testing with realistic assumptions:

**Features:**
- Time-ordered multi-symbol processing
- Shared capital across symbols
- Dynamic position sizing (1.5% risk per trade)
- Circuit breaker (pauses at high drawdown)
- Rolling returns (1M, 3M, 6M, 12M)

**Entry Logic:**
1. Pre-fetch all symbols' 1h candles
2. For each candle timestamp t:
   - Update/close open positions
   - Check new entry signals
   - Lock capital for new positions
3. Force-close remaining positions at end

**Parameters:**
- `--entry-threshold`: Minimum signal score (default 6.5)
- `--fee-pct`: Round-trip fees (default 0.14%)
- `--slippage-pct`: Adverse slippage (default 0.02%)
- `--max-drawdown`: Circuit breaker limit (default 35%)

### Test Suite (tests/)

33 comprehensive tests validating:

**Unit Tests (19):**
- RSI calculation (5 tests)
- EMA responsiveness (4 tests)
- MACD relationships (4 tests)
- ADX trend detection (3 tests)
- Volume ratio detection (3 tests)

**Integration Tests (14):**
- Technical scoring (6 tests)
- TP/SL placement (4 tests)
- Signal generation (5 tests)

## Recent Optimizations

### Forming Candle Fix
- Issue: Scanner was using incomplete current candle
- Fix: Fetch 4001 candles, drop last (incomplete) candle
- Impact: Scanner and backtester now aligned (WYTIWYT)

### Time-Ordered Backtesting
- Issue: Sequential processing allowed same capital to trade multiple times
- Fix: Single unified loop across all symbols with shared capital
- Impact: Realistic capital constraints, honest results

### ADX Multiplier Fix
- Issue: Formula produced negative multipliers
- Fix: Floor at 0.3 minimum
- Impact: ADX always contributes positively to score

### Rolling Returns Feature
- Automatically calculates 1M, 3M, 6M, 12M returns from single backtest
- No need to re-run multiple backtests
- Handles partial periods gracefully

## Performance (Current Configuration — Mar 2026)

**Threshold 6.0 (Recommended):**
- Return: +8.0% (12-month, Apr 2025 - Mar 2026)
- Win Rate: 33.1%
- Profit Factor: 1.07x
- Max Drawdown: 16.9%
- Trades/Month: ~22

**Threshold 7.0 (Conservative):**
- Return: +4.2% (12-month)
- Win Rate: 33.3%
- Profit Factor: 1.07x
- Max Drawdown: 13.1%
- Trades/Month: ~10

See `docs/OPTIMIZATION-V1-BREAKOUT-THRESHOLD.md` for detailed comparison.

## Data Sources

**Market Data:**
- Binance 1h candles (real-time + historical)
- 12+ months history + 50 days warmup

**Fundamental Data:**
- BTC Fear & Greed Index
- Binance funding rates (per-symbol)
- Binance long/short ratios (per-symbol)

**Sentiment Data:**
- HackerNews (crypto-related)
- Medium (crypto publications)
- Kraken Blog (exchange news)

## Running the Bot

### Backtest
```bash
python3 -m trading_bot.backtester --months 12
```

### Live Scanner
```bash
python3 -m trading_bot.scanner
```

### Run Tests
```bash
python3 -m pytest tests/ -v
```

### Example (see examples/run_backtest.sh)
```bash
./examples/run_backtest.sh
```

## Git Workflow

1. **Create feature branch:**
   ```bash
   git checkout -b feature/your-feature
   ```

2. **Make changes, run tests:**
   ```bash
   python3 -m pytest tests/ -v
   ```

3. **Commit with clear messages:**
   ```bash
   git commit -m "feat: add new indicator" -m "Detailed description..."
   ```

4. **Push and open PR (if using GitHub)**

## Development Setup

```bash
# Clone repo
git clone <repo-url>
cd trading-bot

# Install dependencies
pip3 install -r requirements.txt

# Run tests
python3 -m pytest tests/ -v

# Run backtest
python3 -m trading_bot.backtester --months 12
```

## Contributing

See README.md for contribution guidelines.

## License

MIT License - see LICENSE file
