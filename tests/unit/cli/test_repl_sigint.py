"""The REPL SIGINT handler must never paint from inside the signal frame —
a raw write can land mid-escape-sequence and strand a half-painted prompt
region. The user-facing notice is scheduled onto the loop instead."""

import click
import pytest

from cli.commands import chat


class _Loop:
    def __init__(self) -> None:
        self.scheduled = []

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, cb) -> None:
        self.scheduled.append(cb)


def test_first_sigint_schedules_notice_and_raises(monkeypatch):
    loop = _Loop()
    handler = chat._make_repl_sigint_handler(loop, {"n": 0})
    with pytest.raises(KeyboardInterrupt):
        handler(2, None)
    assert len(loop.scheduled) == 1

    writes = []
    monkeypatch.setattr(click, "echo", lambda *a, **k: writes.append(a))
    loop.scheduled[0]()
    assert any("Interrupting (Ctrl+C again to force exit)" in str(a) for a in writes)


def test_second_sigint_is_force_exit():
    loop = _Loop()
    counter = {"n": 0}
    handler = chat._make_repl_sigint_handler(loop, counter)
    with pytest.raises(KeyboardInterrupt):
        handler(2, None)
    with pytest.raises(KeyboardInterrupt):
        handler(2, None)
    assert counter["n"] == 2
    assert len(loop.scheduled) == 2


def test_handler_never_echoes_inline(monkeypatch):
    writes = []
    monkeypatch.setattr(click, "echo", lambda *a, **k: writes.append(a))
    handler = chat._make_repl_sigint_handler(None, {"n": 0})  # no loop → drop notice
    with pytest.raises(KeyboardInterrupt):
        handler(2, None)
    assert writes == []
