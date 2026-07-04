"""getUpdates CONFLICT (another instance polling the same token) must be handled
gracefully — a concise warning + a longer backoff — NOT a full traceback every poll.

Live-caught during the grok-4.3 VPS soak (2026-06-27): an external poller on the
bot token made run_polling dump a traceback every ~1s, flooding the journal.
"""

import asyncio
import logging

from surfaces.telegram.harness import (
    TelegramHarness,
    _is_conflict_error,
    _CONFLICT_BACKOFF_SEC,
)


def _harness_raising(exc):
    h = object.__new__(TelegramHarness)
    h._running = True
    h.poll_timeout = 0

    class _Bot:
        async def get_updates(self, **kw):
            raise exc

    h.bot = _Bot()
    return h


def test_is_conflict_error_detection():
    assert _is_conflict_error(Exception("Conflict: terminated by other getUpdates request"))
    conflict = type("TelegramConflictError", (Exception,), {})("x")
    assert _is_conflict_error(conflict)
    assert not _is_conflict_error(ValueError("transient network blip"))


def test_conflict_logs_warning_and_backs_off(monkeypatch, caplog):
    h = _harness_raising(Exception("Conflict: terminated by other getUpdates request"))
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)
        h._running = False  # break the loop after one iteration

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    with caplog.at_level(logging.WARNING):
        asyncio.run(h.run_polling())

    assert slept == [_CONFLICT_BACKOFF_SEC], "conflict must back off the long interval"
    assert any("conflict" in r.getMessage().lower() for r in caplog.records)
    # A conflict must NOT log at ERROR level / dump a traceback.
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_generic_error_keeps_fast_retry_and_error_log(monkeypatch, caplog):
    h = _harness_raising(ValueError("transient network blip"))
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)
        h._running = False

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    with caplog.at_level(logging.ERROR):
        asyncio.run(h.run_polling())

    assert slept == [1], "a generic transient error keeps the 1s fast retry"
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
