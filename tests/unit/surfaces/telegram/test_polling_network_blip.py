"""A long-poll connection reset is WEATHER, not an incident — until it persists.

Prod, 2026-09-22: `get_updates failed: ... ClientOSError: [Errno 104] Connection
reset by peer` lands roughly once an hour (24 h: 08:51-08:55 ×3, 09:10, 11:59,
12:01), each one an ERROR plus a full traceback that polling recovers from on the
next iteration a second later. That is ~24 tracebacks a day every reader — the
maintenance loop, the intel review, the watchdog, the status snapshot's error
counts — has to recognise and discard, which is exactly how a real one gets
missed.

So: ONE concise WARNING for the first few consecutive blips, and an ERROR with
the traceback once the streak says polling is actually down rather than
blipping. The telemetry record is written either way (it feeds the poll-health
snapshot), and a success resets the streak — the escalation measures a RUN of
failures, never a lifetime total.
"""
import asyncio
import logging

import pytest

from surfaces.telegram.harness import (
    TelegramHarness,
    _is_transient_poll_error,
    _TRANSIENT_ESCALATE_AFTER,
)


class _Reset(OSError):
    """Stands in for aiohttp's ClientOSError without importing aiohttp here."""
    __name__ = "ClientOSError"


def _harness_failing(exc, times):
    """A harness whose get_updates raises *exc* *times* times, then stops."""
    h = object.__new__(TelegramHarness)
    h._running = True
    h.poll_timeout = 0
    left = {"n": times}

    class _Bot:
        async def get_updates(self, **kw):
            if left["n"] <= 0:
                h._running = False
                return []
            left["n"] -= 1
            raise exc

    h.bot = _Bot()
    return h


def _run(h, monkeypatch):
    async def fake_sleep(sec):
        return None
    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    asyncio.run(h.run_polling())


# -- classification ----------------------------------------------------------

def test_a_connection_reset_is_recognised_as_transient():
    assert _is_transient_poll_error(ConnectionResetError(104, "Connection reset by peer"))


def test_aiogram_network_wrapper_is_recognised_by_name():
    net = type("TelegramNetworkError", (Exception,), {})(
        "HTTP Client says - ClientOSError: [Errno 104] Connection reset by peer")
    assert _is_transient_poll_error(net)


def test_a_transient_cause_under_a_plain_exception_is_recognised():
    """aiogram re-raises with the socket error as __cause__."""
    outer = Exception("HTTP Client says")
    outer.__cause__ = ConnectionResetError(104, "Connection reset by peer")
    assert _is_transient_poll_error(outer)


def test_a_programming_error_is_NOT_transient():
    assert not _is_transient_poll_error(ValueError("bad offset"))
    assert not _is_transient_poll_error(KeyError("update_id"))


# -- the log the reader sees -------------------------------------------------

def test_one_blip_logs_a_single_warning_and_no_traceback(monkeypatch, caplog):
    h = _harness_failing(ConnectionResetError(104, "reset"), times=1)
    with caplog.at_level(logging.DEBUG):
        _run(h, monkeypatch)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "reset" in warnings[0].getMessage().lower() or "network" in warnings[0].getMessage().lower()
    assert warnings[0].exc_info is None, "a routine blip carries no traceback"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_sustained_streak_escalates_to_error_with_the_traceback(monkeypatch, caplog):
    h = _harness_failing(ConnectionResetError(104, "reset"),
                         times=_TRANSIENT_ESCALATE_AFTER + 1)
    with caplog.at_level(logging.DEBUG):
        _run(h, monkeypatch)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "polling that stays down must become loud"
    assert errors[0].exc_info is not None, "the escalation carries the traceback"


def test_a_success_resets_the_streak(monkeypatch, caplog):
    """Two blips an hour apart are not a two-deep streak."""
    h = object.__new__(TelegramHarness)
    h._running = True
    h.poll_timeout = 0
    script = ["fail", "ok", "fail", "ok", "fail", "stop"]

    class _Bot:
        async def get_updates(self, **kw):
            step = script.pop(0)
            if step == "fail":
                raise ConnectionResetError(104, "reset")
            if step == "stop":
                h._running = False
            return []

    h.bot = _Bot()
    with caplog.at_level(logging.DEBUG):
        _run(h, monkeypatch)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], \
        "three isolated blips separated by successes never escalate"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 3


def test_every_blip_is_still_recorded_for_the_health_snapshot(monkeypatch):
    """Quieter in the journal must not mean invisible to the poll-health store."""
    seen = []
    monkeypatch.setattr("surfaces.telegram.harness.record_poll_error",
                        lambda sid, exc: seen.append((sid, type(exc).__name__)))
    h = _harness_failing(ConnectionResetError(104, "reset"), times=2)
    _run(h, monkeypatch)
    assert [s for s, _ in seen] == ["telegram", "telegram"]


def test_a_non_transient_error_is_unchanged(monkeypatch, caplog):
    h = _harness_failing(ValueError("bad offset"), times=1)
    with caplog.at_level(logging.DEBUG):
        _run(h, monkeypatch)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and errors[0].exc_info is not None


# --- Telegram's OWN 5xx is weather too (added 2026-09-23) --------------------- #
# Live 01:10:44→01:10:54: nine consecutive `TelegramServerError: Bad Gateway` in
# ten seconds, each an ERROR with a full traceback because the name was missing
# from the transient set — so the streak logic never engaged and every failure
# reset the count. Polling recovered by 01:11:56 with nothing lost.


class _TelegramServerError(Exception):
    """Stands in for aiogram's TelegramServerError without importing aiogram."""


_TelegramServerError.__name__ = "TelegramServerError"


class _TelegramRetryAfter(Exception):
    """Flood control. Transient, but it carries its own retry_after."""


_TelegramRetryAfter.__name__ = "TelegramRetryAfter"


def test_a_telegram_5xx_is_transient():
    assert _is_transient_poll_error(
        _TelegramServerError("Telegram server says - Bad Gateway")) is True


def test_a_wrapped_telegram_5xx_is_transient():
    """aiogram chains; the walk must find it."""
    try:
        try:
            raise _TelegramServerError("Bad Gateway")
        except Exception as inner:
            raise RuntimeError("polling failed") from inner
    except RuntimeError as outer:
        assert _is_transient_poll_error(outer) is True


def test_flood_control_is_NOT_in_the_transient_set():
    """It is transient, but honouring `retry_after` is a different branch.

    Treating it as an ordinary blip would retry after the flat 1 s sleep and
    deepen the limit — so it must not silently ride this path.
    """
    assert _is_transient_poll_error(_TelegramRetryAfter("Flood control exceeded")) is False


def test_a_real_fault_is_still_not_transient():
    class _Unauthorized(Exception):
        pass
    _Unauthorized.__name__ = "TelegramUnauthorizedError"
    assert _is_transient_poll_error(_Unauthorized("bot token is invalid")) is False
