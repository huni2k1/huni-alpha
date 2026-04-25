# Trading Bot Tests

This test suite covers the current package-based repo layout under `src/trading_bot/`.
Older references to root-level scripts such as `scanner/market-scanner.py` are stale and no
longer apply here.

## Run The Suite

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

If you are using the local project virtualenv:

```bash
PYTHONPATH=src .venv/bin/pytest -q -p no:cacheprovider
```

## Coverage

```bash
PYTHONPATH=src .venv/bin/pytest -q -p no:cacheprovider \
  --cov=src/trading_bot --cov-report=term-missing
```

Current suite size is around `232` tests, not the older `33`-test layout.

## Main Test Areas

- `test_indicators.py`
  Basic indicator math for RSI, EMA, MACD, ADX, Bollinger, and volume helpers.
- `test_signals.py`
  Scanner signal generation and TP/SL consistency.
- `test_scanner_advanced.py`
  Scanner behavior under mocked technical conditions and filter paths.
- `test_backtester_core.py`
  Core fill, TP/SL, trailing stop, and accounting behavior.
- `test_backtester_next_open.py`
  Next-open execution behavior, including timestamp correctness.
- `test_backtester_cache.py`
  Candle cache separation between scanner and backtester variants.
- `test_statistical_signal_mode.py`
  Rulebook/statistical signal flow and backtester integration.
- `test_reconcile.py`
  Trader state reconciliation and exchange sync behavior.
- `test_logging_notifications.py`
  Live logging and Telegram notification formatting.
- `test_runtime_smoke.py`
  Startup/runtime smoke coverage for the trader loop.

## Notes

- Many tests import modules with `importlib.util.spec_from_file_location(...)` so they can
  exercise package files directly.
- Cache-focused tests assume the cache lives in `candle_cache.py`; manual cache management is
  exposed through `python3 -m trading_bot.cache_manager`.
- For the most realistic validation, favor integration tests around `scanner.py`,
  `backtester.py`, and `trader.py`.
