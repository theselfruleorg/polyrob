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
    from core import event_log as el
    from core import tickers

    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)

    class _Task:
        def done(self):
            return False

    monkeypatch.setattr(lw, "take_window", lambda: {"lag_ms_max": 42, "stalls": 0, "site": None})
    tickers.emit_loop_heartbeats([("cron", _Task())])
    monkeypatch.setattr(lw, "take_window", lambda: None)
    tickers.emit_loop_heartbeats([("cron", _Task())])
    newest, first = [r["attrs"] for r in log.query(kind="autonomy_tick")]
    assert first["loop_lag_ms_max"] == 42 and first["loop_stalls"] == 0
    assert "loop_lag_ms_max" not in newest
    # 116 W4.1: every row names its process and its emission.
    import os
    assert first["pid"] == os.getpid() and first["pid_start"] and first["hb"]


def _tick(ts, **attrs):
    return {"ts": ts, "attrs": {"loop": "cron", **attrs}}


def test_status_says_not_measured_and_warns_over_a_second():
    from core.status_loops import _loop_lag_lines
    from core.status_types import Section

    sec = Section(name="loops")
    _loop_lag_lines(sec, [_tick(1)])
    assert sec.lines == ["event-loop lag: not measured"] and not sec.health

    sec = Section(name="loops")
    _loop_lag_lines(sec, [_tick(1, loop_lag_ms_max=40, hb=1, pid=1)])
    assert "max 40 ms in 24h, 0 stall(s) over 1 s" in sec.lines[0] and not sec.health


def test_the_lag_is_the_maximum_of_the_window_not_the_newest_value():
    """116 W4.3: prod said `max 2035 ms` on a day with stalls to 25 s."""
    from core.status_loops import _loop_lag_lines
    from core.status_types import Section

    rows = [  # newest first; one emission writes the same window on two rows
        _tick(900, loop_lag_ms_max=200, loop_stalls=0, hb=3, pid=7),
        {"ts": 900, "attrs": {"loop": "goals", "loop_lag_ms_max": 200, "loop_stalls": 0,
                              "hb": 3, "pid": 7}},
        _tick(600, loop_lag_ms_max=25200, loop_stalls=4, hb=2, pid=7,
              loop_stall_site="task/workspace_context.py:218 in get_workspace_changes"),
        {"ts": 600, "attrs": {"loop": "goals", "loop_lag_ms_max": 25200, "loop_stalls": 4,
                              "hb": 2, "pid": 7}},
        _tick(300, loop_lag_ms_max=1500),               # an old row: no stall count
    ]
    sec = Section(name="loops")
    _loop_lag_lines(sec, rows)
    assert sec.data["loop_lag_ms_max"] == 25200
    assert sec.lines == ["event-loop lag: max 25.2 s in 24h, 5 stall(s) over 1 s "
                         "(latest window 200 ms)"]
    assert sec.health[0].key == "loop_lag"
    assert "workspace_context.py:218" in sec.health[0].text


def test_the_watch_counts_stalls_and_names_our_frame():
    import sys
    watch = lw._Watch(None, 0)
    watch.lag_max_s, watch.stalls = 1.2, 2
    watch.site = lw._own_site(sys._getframe())
    assert "test_loop_watch.py" in watch.site
    old, lw._current = lw._current, watch
    try:
        win = lw.take_window()
        assert win["lag_ms_max"] == 1200 and win["stalls"] == 2 and win["site"]
        assert lw.take_window() is None                 # reset: not measured again
    finally:
        lw._current = old
