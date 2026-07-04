"""Bug E: the bootstrap 'starting…' notice is transient on a TTY (erased once
the banner is ready) and a plain line otherwise."""

from __future__ import annotations

from io import StringIO

from cli.ui import bootstrap_notice as bn


class _FakeTTY(StringIO):
    def isatty(self) -> bool:  # noqa: D401
        return True


def test_transient_on_tty_no_newline_then_cleared(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    s = _FakeTTY()
    transient = bn.show_start_notice(s)
    assert transient is True
    assert s.getvalue() == "starting…"  # no trailing newline → erasable in place
    bn.clear_start_notice(s, transient)
    assert s.getvalue() == "starting…\r\x1b[K"  # CR + clear-to-EOL erases it


def test_plain_stream_writes_a_line_and_clear_is_noop():
    s = StringIO()  # isatty() -> False
    transient = bn.show_start_notice(s)
    assert transient is False
    assert s.getvalue() == "starting…\n"
    bn.clear_start_notice(s, transient)
    assert s.getvalue() == "starting…\n"  # unchanged — no ANSI junk on a pipe


def test_no_color_tty_is_not_transient(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    s = _FakeTTY()
    transient = bn.show_start_notice(s)
    assert transient is False
    assert s.getvalue() == "starting…\n"
