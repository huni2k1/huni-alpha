"""
Pytest configuration and shared fixtures for trading bot tests.
"""
import pytest
import importlib.util
import os


@pytest.fixture(scope="session")
def market_scanner():
    """Load market-scanner.py module for all tests."""
    scanner_path = os.path.join(os.path.dirname(__file__), '..', 'scanner', 'market-scanner.py')
    spec = importlib.util.spec_from_file_location("market_scanner", scanner_path)
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    return scanner


@pytest.fixture
def trending_candles_up():
    """Generate 1000 candles in a clean uptrend."""
    candles = []
    price = 100.0
    for i in range(1000):
        price += 0.15
        candles.append([
            price - 0.05,   # open
            price + 0.10,   # high
            price - 0.10,   # low
            price,          # close
            1000.0 + (i % 50) * 20  # volume
        ])
    return candles


@pytest.fixture
def trending_candles_down():
    """Generate 1000 candles in a clean downtrend."""
    candles = []
    price = 100.0
    for i in range(1000):
        price -= 0.15
        candles.append([
            price - 0.05,   # open
            price + 0.10,   # high
            price - 0.10,   # low
            price,          # close
            1000.0 + (i % 50) * 20  # volume
        ])
    return candles


@pytest.fixture
def choppy_candles():
    """Generate 1000 candles in a sideways/choppy market."""
    import random
    random.seed(42)
    candles = []
    price = 100.0
    for i in range(1000):
        change = random.uniform(-0.3, 0.3)
        price = max(90.0, min(110.0, price + change))
        candles.append([
            price - 0.05,
            price + 0.15,
            price - 0.15,
            price,
            1000.0
        ])
    return candles
