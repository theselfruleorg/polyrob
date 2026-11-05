"""102 C-13: a sync stall stops every session, and nothing else can report it."""
import asyncio
import logging
import time

import pytest

import core.loop_watch as lw


@pytest.fixture(autouse=True)
def _reset():
    lw._current = None
    yield
    if lw._current is not None:
        lw._current.stop.set()
    lw._current = None


def _blocking_call_for_the_test(seconds):
    time.sleep(seconds)


def test_no_watch_is_not_measured():
    assert lw.take_lag_ms_max() is None


def test_a_stall_is_measured_and_the_read_resets_it():
    async def go():
        lw.start()
        await asyncio.sleep(lw.BEAT_S + 0.05)
        _blocking_call_for_the_test(1.2)
        await asyncio.sleep(lw.BEAT_S + 0.1)
        first = lw.take_lag_ms_max()
        await asyncio.sleep(lw.BEAT_S + 0.1)
        second = lw.take_lag_ms_max()
        return first, second

    first, second = asyncio.run(go())
    assert first >= 900
    assert second < 500


def test_a_long_stall_logs_the_blocking_frame(caplog, monkeypatch):
    monkeypatch.setattr(lw, "STALL_S", 0.3)
    monkeypatch.setattr(lw, "POLL_S", 0.05)

    async def go():
        lw.start()
        await asyncio.sleep(0.01)
        _blocking_call_for_the_test(0.8)
        await asyncio.sleep(lw.BEAT_S + 0.05)

    with caplog.at_level(logging.WARNING, logger="core.loop_watch"):
        asyncio.run(go())
    stuck = [r.getMessage() for r in caplog.records if "stuck" in r.getMessage()]
    assert len(stuck) == 1
    assert "_blocking_call_for_the_test" in stuck[0]
    assert any("stalled for" in r.getMessage() for r in caplog.records)


def test_the_second_start_on_a_loop_does_nothing_and_a_new_loop_restarts():
    seen = []

    async def go():
        lw.start()
        seen.append(lw._current)
        lw.start()
        seen.append(lw._current)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert seen[0] is seen[1]
    assert seen[0].stop.is_set()  # the beat ended with its loop
    assert lw.take_lag_ms_max() is None
    asyncio.run(go())
    assert seen[2] is not seen[0]


def test_the_heartbeat_row_carries_the_lag(monkeypatch, tmp_path):
    from core import tickers

    rows = []

    class _Log:
        def record(self, kind, **kw):
            rows.append(kw)

        def prune(self, **kw):
            pass

    monkeypatch.setattr("core.event_log.get_event_log", lambda: _Log())
    monkeypatch.setattr("core.event_log.event_log_enabled", lambda: True)

    class _Task:
        def done(self):
            return False

    monkeypatch.setattr(lw, "take_lag_ms_max", lambda: 42)
    tickers.emit_loop_heartbeats([("cron", _Task())])
    assert rows[0]["loop_lag_ms_max"] == 42
    monkeypatch.setattr(lw, "take_lag_ms_max", lambda: None)
    tickers.emit_loop_heartbeats([("cron", _Task())])
    assert "loop_lag_ms_max" not in rows[1]


def test_status_says_not_measured_and_warns_over_a_second():
    from core.status_loops import _loop_lag_lines
    from core.status_types import Section

    sec = Section(name="loops")
    _loop_lag_lines(sec, {"cron": {"ts": 1, "alive": True, "lag": None}})
    assert sec.lines == ["event-loop lag: not measured"] and not sec.health

    sec = Section(name="loops")
    _loop_lag_lines(sec, {"cron": {"ts": 1, "alive": True, "lag": 40}})
    assert "max 40 ms" in sec.lines[0] and not sec.health

    sec = Section(name="loops")
    _loop_lag_lines(sec, {"cron": {"lag": 1500}, "goals": {"lag": 2}})
    assert sec.data["loop_lag_ms_max"] == 1500
    assert sec.health[0].key == "loop_lag"
