import json

from trading_bot import candle_cache


def _cache_file(tmp_path, symbol="BTCUSDT", interval="1h", start="2026-01-01", end="2026-01-02", variant=None):
    candle_cache.CACHE_DIR = str(tmp_path)
    if variant is None:
        return candle_cache._get_cache_path(symbol, interval, start, end)
    return candle_cache._get_cache_path(symbol, interval, start, end, variant=variant)


def test_load_from_cache_returns_none_for_missing_file(tmp_path):
    candle_cache.CACHE_DIR = str(tmp_path)

    assert candle_cache.load_from_cache("BTCUSDT", "1h", "2026-01-01", "2026-01-02") is None


def test_load_from_cache_returns_recent_complete_data(tmp_path, monkeypatch):
    path = _cache_file(tmp_path)
    candles = [[1, 2, 3, 4, 5] for _ in range(24)]
    monkeypatch.setattr(candle_cache.time, "time", lambda: 2000.0)
    with open(path, "w") as f:
        json.dump({"cached_at": 1999.0, "candles": candles}, f)

    assert candle_cache.load_from_cache("BTCUSDT", "1h", "2026-01-01", "2026-01-02") == candles


def test_load_from_cache_rejects_stale_complete_cache(tmp_path, monkeypatch, capsys):
    path = _cache_file(tmp_path)
    candles = [[1, 2, 3, 4, 5] for _ in range(24)]
    monkeypatch.setattr(candle_cache.time, "time", lambda: 2000.0 + 25 * 3600)
    with open(path, "w") as f:
        json.dump({"cached_at": 2000.0, "candles": candles}, f)

    assert candle_cache.load_from_cache("BTCUSDT", "1h", "2026-01-01", "2026-01-02") is None
    assert "complete but stale" in capsys.readouterr().out


def test_load_from_cache_rejects_incomplete_cache(tmp_path, monkeypatch):
    path = _cache_file(tmp_path)
    monkeypatch.setattr(candle_cache.time, "time", lambda: 2000.0)
    with open(path, "w") as f:
        json.dump({"cached_at": 1999.0, "candles": [[1, 2, 3, 4, 5]]}, f)

    assert candle_cache.load_from_cache("BTCUSDT", "1h", "2026-01-01", "2026-01-02") is None


def test_load_from_cache_handles_corrupt_json(tmp_path, capsys):
    path = _cache_file(tmp_path)
    with open(path, "w") as f:
        f.write("{not-json")

    assert candle_cache.load_from_cache("BTCUSDT", "1h", "2026-01-01", "2026-01-02") is None
    assert "Error loading" in capsys.readouterr().out


def test_save_to_cache_writes_metadata(tmp_path):
    path = _cache_file(tmp_path, variant="custom")
    candles = [[1, 2, 3, 4, 5]]

    assert candle_cache.save_to_cache("BTCUSDT", "1h", "2026-01-01", "2026-01-02", candles, variant="custom")

    with open(path) as f:
        data = json.load(f)
    assert data["schema_version"] == candle_cache.SCHEMA_VERSION
    assert data["variant"] == "custom"
    assert data["count"] == 1
    assert data["candles"] == candles


def test_save_to_cache_returns_false_on_write_error(monkeypatch, tmp_path, capsys):
    candle_cache.CACHE_DIR = str(tmp_path)

    def fail_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", fail_open)

    ok = candle_cache.save_to_cache("BTCUSDT", "1h", "2026-01-01", "2026-01-02", [])

    assert ok is False
    assert "Error saving" in capsys.readouterr().out


def test_get_cache_stats_returns_zero_when_dir_missing(tmp_path):
    candle_cache.CACHE_DIR = str(tmp_path / "missing")

    assert candle_cache.get_cache_stats() == {"total_files": 0, "total_size_mb": 0, "symbols": []}


def test_get_cache_stats_counts_json_files_only(tmp_path):
    candle_cache.CACHE_DIR = str(tmp_path)
    btc_path = _cache_file(tmp_path, symbol="BTCUSDT")
    eth_path = _cache_file(tmp_path, symbol="ETHUSDT", interval="4h", start="2026-01-01", end="2026-01-03")
    with open(btc_path, "w") as f:
        json.dump({"candles": []}, f)
    with open(eth_path, "w") as f:
        json.dump({"candles": []}, f)
    with open(tmp_path / "BTCUSDT" / "1h" / "ignore.txt", "w") as f:
        f.write("x")

    stats = candle_cache.get_cache_stats()

    assert stats["total_files"] == 2
    assert stats["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert stats["total_size_mb"] >= 0


def test_clear_cache_for_symbol_removes_symbol_dir(tmp_path):
    path = _cache_file(tmp_path, symbol="BTCUSDT")
    with open(path, "w") as f:
        json.dump({"candles": []}, f)

    assert candle_cache.clear_cache("BTCUSDT") is True
    assert not (tmp_path / "BTCUSDT").exists()


def test_clear_cache_all_recreates_root_dir(tmp_path):
    path = _cache_file(tmp_path, symbol="BTCUSDT")
    with open(path, "w") as f:
        json.dump({"candles": []}, f)

    assert candle_cache.clear_cache() is True
    assert tmp_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_clear_cache_returns_false_on_delete_error(monkeypatch, tmp_path, capsys):
    path = _cache_file(tmp_path, symbol="BTCUSDT")
    with open(path, "w") as f:
        json.dump({"candles": []}, f)

    class _FailingShutil:
        @staticmethod
        def rmtree(_path):
            raise OSError("permission denied")

    import sys
    monkeypatch.setitem(sys.modules, "shutil", _FailingShutil)

    assert candle_cache.clear_cache("BTCUSDT") is False
    assert "Error clearing cache" in capsys.readouterr().out
