# Huni Alpha

Multi-regime crypto signal generator and backtester built around a single shared signal contract.

The project has two main runtime paths:

- `src/trading_bot/scanner.py` runs the live scanner, fetches fresh 1h candles, scores setups, and sends Telegram alerts.
- `src/trading_bot/backtester.py` reuses the scanner's signal logic in tech-only mode to simulate historical trades with shared capital, fees, slippage, cooldowns, and rolling-return stats.

## Current Status

This README reflects the code that is currently in `src/trading_bot/`. Older docs and commands that referenced root-level scripts like `scanner/market-scanner.py` and `backtester.py` are no longer accurate for this repo layout.

## Quick Start

### Live Scanner

The live scanner uses public market-data endpoints, so Binance trading credentials are not required for signal generation.

Set Telegram credentials through environment variables before starting:

```bash
cd /Users/ninhvan/.openclaw/workspace/trading-bot
export TELEGRAM_TOKEN="..."
export TELEGRAM_CHAT="..."
python3 src/trading_bot/scanner.py
```

Useful logs:

```bash
tail -f /tmp/scanner.log
tail -f /tmp/scanner-debug.log
```

### Historical Backtest

Run the packaged backtester directly from the current source tree:

```bash
cd /Users/ninhvan/.openclaw/workspace/trading-bot
python3 src/trading_bot/backtester.py --months 6 --account 1000
```

Common examples:

```bash
# 12-month run with current strategy defaults
python3 src/trading_bot/backtester.py --months 12 --account 1000

# Override strategy-specific thresholds
python3 src/trading_bot/backtester.py --months 12 --trend-threshold 7.0 --breakout-threshold 6.0

# Reset to starting balance each month
python3 src/trading_bot/backtester.py --months 12 --reset-monthly

# Disable Kelly sizing and use flat risk sizing
python3 src/trading_bot/backtester.py --months 12 --no-kelly-sizing
```

By default, JSON output is written under `src/trading_bot/backtest-results.json`.

### Cache Manager

The cache manager is exposed as a module:

```bash
cd /Users/ninhvan/.openclaw/workspace/trading-bot
PYTHONPATH=src python3 -m trading_bot.cache_manager stats
PYTHONPATH=src python3 -m trading_bot.cache_manager clear BTCUSDT
```

## Strategy Overview

The scanner works on 1h candles and scores two active setup families:

- `trend_pullback`: momentum continuation with regime-aware RSI zones, MACD confirmation, EMA alignment, and an EMA200-style macro filter.
- `breakout`: squeeze-release entries using Bollinger expansion, volume, ADX confirmation, and market-structure checks.

Current score layers:

- Technical: direction bias, regime detection, RSI, MACD, ADX, EMA alignment, volume.
- Fundamentals: fear and greed, funding rate, open interest metadata, long/short ratio.
- News: RSS headline sentiment with symbol-keyword filtering.

Current live alert thresholds in `scanner.py`:

- Watch: `5.5+`
- Entry: `6.0+`
- High confidence: `7.5+`

Current strategy thresholds shared with the backtester:

- Trend pullback: `7.0+`
- Breakout: `6.0+`

## Risk Management

Trade construction is ATR-based rather than fixed-percentage based.

- Trend pullback uses `1.5x ATR` stop distance and `2.0:1` reward/risk.
- Breakout uses `2.0x ATR` stop distance and `2.5:1` reward/risk.
- Position sizing defaults to `1.5%` risk per trade and can scale with Kelly sizing.
- Backtests can cap concurrent positions, apply per-side slippage, enforce cooldowns, and trigger a drawdown circuit breaker.

## Architecture

### Single Signal Contract

`generate_signal()` in `src/trading_bot/scanner.py` is the shared contract for both live scanning and backtesting. It returns:

- direction
- total score
- technical/fundamental/news breakdown
- ATR-derived entry, take-profit, and stop-loss
- regime and strategy metadata

### Live Path

The live scanner:

1. fetches `4001` 1h candles,
2. drops the last forming candle,
3. evaluates the completed history,
4. applies alert thresholds and cooldown gating,
5. emits Telegram messages and log summaries.

### Backtest Path

The backtester:

1. imports scanner functions dynamically,
2. fetches and caches historical candles,
3. walks forward candle by candle across all symbols,
4. updates open positions before checking new entries,
5. enters at the next candle open,
6. records equity, rolling returns, trade durations, and per-symbol/per-strategy stats.

Backtests run in tech-only mode. Fundamentals and news are skipped intentionally because the project does not maintain historical archives for those inputs.

## Files and Layout

```text
trading-bot/
├── config/
│   ├── binance-real.json
│   └── telegram.json
├── docs/
├── src/
│   └── trading_bot/
│       ├── __init__.py
│       ├── backtester.py
│       ├── cache_manager.py
│       ├── candle_cache.py
│       └── scanner.py
├── tests/
│   ├── conftest.py
│   ├── test_backtester_core.py
│   ├── test_backtester_scoreband.py
│   ├── test_critical_filters.py
│   ├── test_indicators.py
│   ├── test_scanner_advanced.py
│   └── test_signals.py
├── requirements.txt
└── README.md
```

## Runtime Files

- Scanner log: `/tmp/scanner.log`
- Scanner debug log: `/tmp/scanner-debug.log`
- Scanner state: `/tmp/scanner-state.json`
- Candle cache: `~/.trading_bot_cache/`
- Default backtest JSON output: `src/trading_bot/backtest-results.json`

## Testing

Run the full suite:

```bash
cd /Users/ninhvan/.openclaw/workspace/trading-bot
python3 -m pytest tests -q
```

Targeted runs:

```bash
python3 -m pytest tests/test_indicators.py -q
python3 -m pytest tests/test_signals.py -q
python3 -m pytest tests/test_scanner_advanced.py -q
python3 -m pytest tests/test_backtester_core.py -q
```

The test suite covers:

- indicator math,
- signal generation and TP/SL consistency,
- scanner filters and regime detection,
- backtester accounting, cooldowns, Kelly sizing, and trailing-stop behavior,
- score-band aggregation.

## CI/CD Deployment

The recommended production path for this repo is:

- GitHub Actions runs tests on every push and pull request.
- Pushes to `main` deploy to a DigitalOcean droplet over SSH after tests pass.
- The droplet runs the live trader via `systemd`.

Deployment assets live in [`deploy/`](/Users/ninhvan/.openclaw/workspace/trading-bot/deploy):

- [`deploy.yml`](/Users/ninhvan/.openclaw/workspace/trading-bot/.github/workflows/deploy.yml)
- [`deploy.sh`](/Users/ninhvan/.openclaw/workspace/trading-bot/deploy/deploy.sh)
- [`huni-alpha.service`](/Users/ninhvan/.openclaw/workspace/trading-bot/deploy/huni-alpha.service)

GitHub Actions secrets required:

- `DROPLET_HOST`
- `DROPLET_USER`
- `DROPLET_SSH_KEY`

Suggested droplet layout:

```bash
sudo mkdir -p /opt/huni-alpha
sudo chown -R "$USER":"$USER" /opt/huni-alpha
git clone git@github.com:huni2k1/trading-bot.git /opt/huni-alpha
cd /opt/huni-alpha
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
```

Install the service:

```bash
sudo cp deploy/huni-alpha.service /etc/systemd/system/huni-alpha.service
sudo systemctl daemon-reload
sudo systemctl enable huni-alpha
sudo systemctl start huni-alpha
```

Put live credentials on the droplet only:

- `config/binance-trading.json`
- `config/telegram.json`

Then validate the bot locally on the droplet:

```bash
cd /opt/huni-alpha
PYTHONPATH=/opt/huni-alpha/src /opt/huni-alpha/.venv/bin/python -u -c 'from trading_bot import trader; trader.main()'
```

## Dependencies

Install the Python dependencies listed in `requirements.txt`:

```bash
cd /Users/ninhvan/.openclaw/workspace/trading-bot
python3 -m pip install -r requirements.txt
```

## Notes

- `scanner.py` and `backtester.py` can both be run directly from `src/trading_bot/`.
- `cache_manager.py` should be run as a module with `PYTHONPATH=src`.
- The backtester logs its configuration and timing summary to stdout before writing the JSON report.
