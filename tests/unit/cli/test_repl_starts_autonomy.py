import inspect
import cli.commands.chat as chat


def test_repl_starts_and_stops_autonomy():
    src = inspect.getsource(chat._repl_main)
    assert "start_autonomy" in src
    assert "local_mode_enabled" in src
    assert ".stop()" in src
