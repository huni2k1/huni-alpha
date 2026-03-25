"""Trading Bot - Multi-regime technical analysis strategy"""

__version__ = "1.0.0"
__author__ = "Trading Bot Team"

from .backtester import run_backtest, print_summary
from .scanner import generate_signal, score_technical

__all__ = [
    "run_backtest",
    "print_summary",
    "generate_signal",
    "score_technical",
]
