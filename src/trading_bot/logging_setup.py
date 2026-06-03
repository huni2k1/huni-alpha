"""Logging configuration for the trading bot.

Module-level setup is bare: get the logger and configure its level. NO
filesystem I/O at import time. Handler creation (which opens log files
and creates dirs) happens only inside configure_logging() — call it
from each app's main(). This keeps the backtester from needing to
monkeypatch os.makedirs at import.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional


LOG_FILE = os.environ.get("TRADING_BOT_LOG", "/tmp/trading-bot.log")
DEBUG_LOG_FILE = os.environ.get("TRADING_BOT_DEBUG_LOG", "/tmp/trading-bot-debug.log")


log = logging.getLogger("trading-bot")
log.setLevel(logging.DEBUG)  # capture everything, filter by handler level
log.propagate = False
dbg = log  # legacy alias used throughout the module

_logging_configured = False


def configure_logging(log_file: Optional[str] = None, debug_log_file: Optional[str] = None) -> None:
    """Attach the rotating file handlers (and optional console handler) to the
    module logger. Idempotent — safe to call multiple times.

    Call this from the entry point of each app (trader.main, backtester CLI).
    Tests can skip it; the logger will silently drop INFO/DEBUG.
    """
    global _logging_configured
    if _logging_configured:
        return

    log_file = log_file or LOG_FILE
    debug_log_file = debug_log_file or DEBUG_LOG_FILE
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if os.path.dirname(debug_log_file):
        os.makedirs(os.path.dirname(debug_log_file), exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = RotatingFileHandler(log_file, maxBytes=50 * 1024 * 1024, backupCount=10)
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    if sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        log.addHandler(sh)

    dfh = RotatingFileHandler(debug_log_file, maxBytes=50 * 1024 * 1024, backupCount=10)
    dfh.setLevel(logging.DEBUG)
    dfh.setFormatter(fmt)
    log.addHandler(dfh)

    _logging_configured = True
