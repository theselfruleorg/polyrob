"""Terminal tab title on launch — the tab must say polyrob+version, not "Python"."""
import io

from cli.ui.terminal_title import clear_terminal_title, set_terminal_title


class _TtyStream(io.StringIO):
    def isatty(self):
        return True


def test_set_writes_osc_title_on_tty(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    s = _TtyStream()
    assert set_terminal_title("polyrob 0.10.0", s) is True
    assert s.getvalue() == "\x1b]0;polyrob 0.10.0\x07"


def test_set_skips_non_tty(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    s = io.StringIO()  # isatty() is False
    assert set_terminal_title("polyrob", s) is False
    assert s.getvalue() == ""


def test_set_skips_dumb_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    s = _TtyStream()
    assert set_terminal_title("polyrob", s) is False
    assert s.getvalue() == ""


def test_clear_resets_title(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    s = _TtyStream()
    clear_terminal_title(s)
    assert s.getvalue() == "\x1b]0;\x07"


def test_never_raises_on_broken_stream(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")

    class _Broken:
        def isatty(self):
            return True

        def write(self, _):
            raise OSError("gone")

    assert set_terminal_title("x", _Broken()) is False
    clear_terminal_title(_Broken())  # must not raise


def test_repl_launch_sets_versioned_title(monkeypatch):
    """_start_repl names the tab before the heavy imports and clears it on exit."""
    import cli.polyrob as p

    calls = []
    monkeypatch.setattr(
        "cli.ui.terminal_title.set_terminal_title",
        lambda text, stream=None: calls.append(("set", text)) or True,
    )
    monkeypatch.setattr(
        "cli.ui.terminal_title.clear_terminal_title",
        lambda stream=None: calls.append(("clear", None)),
    )
    monkeypatch.setattr(
        "cli.commands.chat.run_repl",
        lambda plain=False, **kw: calls.append(("repl", None)),
    )
    p._start_repl(plain=False, model=None, provider=None, toolset=None)
    assert calls[0] == ("set", f"polyrob {p.VERSION}")
    assert ("repl", None) in calls
    assert calls[-1] == ("clear", None)
