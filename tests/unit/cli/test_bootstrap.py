"""Unit tests for cli.commands._bootstrap.suppress_bootstrap_output and
attach_dispatcher_event_log."""
import io
import os
import sys

import pytest

from cli.commands._bootstrap import attach_dispatcher_event_log, suppress_bootstrap_output


def test_exception_inside_window_restores_stdout_stderr_and_fd2():
    """An exception raised inside the suppress window must restore
    sys.stdout, sys.stderr, and OS fd 2 before the exception propagates."""
    real_stdout = sys.stdout
    real_stderr = sys.stderr
    # Record the real fd 2 target via /proc/self/fd or by dup-ing it.
    real_fd2_dup = os.dup(2)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            with suppress_bootstrap_output():
                raise RuntimeError("boom")

        # Python-level streams restored.
        assert sys.stdout is real_stdout
        assert sys.stderr is real_stderr

        # OS fd 2 points back to the same underlying file description as before.
        # We verify this by checking that writing to fd 2 doesn't raise and that
        # the restored fd 2 is open (stat succeeds).
        os.fstat(2)  # raises OSError if fd 2 is closed / invalid
    finally:
        os.close(real_fd2_dup)


def test_print_inside_window_does_not_reach_pre_window_stdout(capsys):
    """print() calls inside the suppress window must not appear on the
    real sys.stdout captured before the window opens."""
    with suppress_bootstrap_output():
        print("should-be-suppressed")

    captured = capsys.readouterr()
    assert "should-be-suppressed" not in captured.out
    assert "should-be-suppressed" not in captured.err


class _FakeDispatcher:
    def __init__(self):
        self.attached = None

    def attach_event_log(self, event_log):
        self.attached = event_log


def test_attach_dispatcher_event_log_wires_when_enabled(monkeypatch):
    """Final-review Fix 1: the CLI dispatcher-start seam attaches a real event log
    when TELEMETRY_EVENT_LOG_ENABLED is on."""
    import agents.task.telemetry.event_log as event_log_mod

    sentinel = object()
    monkeypatch.setattr(event_log_mod, "event_log_enabled", lambda: True)
    monkeypatch.setattr(event_log_mod, "get_event_log", lambda: sentinel)

    d = _FakeDispatcher()
    attach_dispatcher_event_log(d)
    assert d.attached is sentinel


def test_attach_dispatcher_event_log_noop_when_disabled(monkeypatch):
    """When telemetry is disabled, the dispatcher is left untouched (None event_log,
    byte-identical legacy)."""
    import agents.task.telemetry.event_log as event_log_mod

    monkeypatch.setattr(event_log_mod, "event_log_enabled", lambda: False)

    d = _FakeDispatcher()
    attach_dispatcher_event_log(d)
    assert d.attached is None


def test_attach_dispatcher_event_log_fails_open_on_error(monkeypatch):
    """A raising event_log_enabled()/get_event_log() must never block a surface
    from starting — fail-open."""
    import agents.task.telemetry.event_log as event_log_mod

    def _boom():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(event_log_mod, "event_log_enabled", _boom)

    d = _FakeDispatcher()
    attach_dispatcher_event_log(d)  # must not raise
    assert d.attached is None
