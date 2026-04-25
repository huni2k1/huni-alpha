from trading_bot import cache_manager


class _Args:
    def __init__(self, symbol=None):
        self.symbol = symbol


def test_cmd_stats_prints_cache_summary(monkeypatch, capsys):
    monkeypatch.setattr(
        cache_manager.candle_cache,
        "get_cache_stats",
        lambda: {"total_files": 3, "total_size_mb": 12.5, "symbols": ["BTCUSDT", "ETHUSDT"]},
    )

    cache_manager.cmd_stats(_Args())

    out = capsys.readouterr().out
    assert "CANDLE CACHE STATISTICS" in out
    assert "Total cached files:  3" in out
    assert "12.50 MB" in out
    assert "BTCUSDT, ETHUSDT" in out


def test_cmd_clear_symbol_clears_without_prompt(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cache_manager.candle_cache, "clear_cache", lambda symbol=None: seen.append(symbol) or True)

    cache_manager.cmd_clear(_Args(symbol="BTCUSDT"))

    out = capsys.readouterr().out
    assert seen == ["BTCUSDT"]
    assert "Clearing cache for BTCUSDT..." in out
    assert "Cache cleared successfully" in out


def test_cmd_clear_all_cancelled_by_user(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    called = []
    monkeypatch.setattr(cache_manager.candle_cache, "clear_cache", lambda symbol=None: called.append(symbol) or True)

    cache_manager.cmd_clear(_Args())

    out = capsys.readouterr().out
    assert called == []
    assert "Clearing ALL cache..." in out
    assert "Cancelled." in out


def test_cmd_clear_all_confirmed_calls_clear(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    called = []
    monkeypatch.setattr(cache_manager.candle_cache, "clear_cache", lambda symbol=None: called.append(symbol) or True)

    cache_manager.cmd_clear(_Args())

    out = capsys.readouterr().out
    assert called == [None]
    assert "Cache cleared successfully" in out


def test_main_dispatches_stats_command(monkeypatch):
    called = []
    monkeypatch.setattr(cache_manager, "cmd_stats", lambda args: called.append(("stats", args.command)))
    monkeypatch.setattr(cache_manager, "cmd_clear", lambda args: called.append(("clear", args.command)))
    monkeypatch.setattr("sys.argv", ["cache_manager.py", "stats"])

    cache_manager.main()

    assert called == [("stats", "stats")]


def test_main_prints_help_when_no_command(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["cache_manager.py"])

    cache_manager.main()

    out = capsys.readouterr().out
    assert "Manage candle cache" in out
    assert "stats" in out
    assert "clear" in out
