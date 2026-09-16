"""Loop liveness unions LIVE processes, never "whoever wrote last" (043 T3).

`autonomy_started` is written once per process start, and the snapshot read the
NEWEST row from ANY process as its `expected` loop set. On a box where the owner
opens a `rob` REPL beside the service — which local mode is designed for — the
REPL's row is newer and names ITS loop set, so the service's curator/settlement/
bridge loops quietly stopped being expected. Nothing rendered differently; the
checks simply stopped happening, which is the silent-narrowing shape a status
surface may never have.

The fix is two halves, both pinned here:

* the WRITER stamps `pid` (and a descriptive `role`) onto the row, so a row can
  be attributed to a process at all;
* the READER groups the recent rows by pid, keeps each group's newest row, drops
  a group whose pid is provably gone, and UNIONS what survives with the
  gate-derived pair. A row with no pid is legacy and is always kept.

Union, never replace: the set may only ever be too WIDE (an extra loop reported
silent), never too narrow (a dead loop nobody checked).
"""
import os
import time

import pytest

from core.event_log import TelemetryEventLog

#: Above Linux's default `pid_max` (2^22) and far above macOS's, so it can never
#: name a running process. Verified per-run by `_dead_pid` below rather than
#: assumed — a "dead" pid that is actually alive would pass this test for the
#: wrong reason.
DEAD_PID = 2 ** 22 + 7


def _dead_pid() -> int:
    try:
        os.kill(DEAD_PID, 0)
    except ProcessLookupError:
        return DEAD_PID
    except Exception:
        pytest.skip("cannot probe pid liveness on this platform")
    pytest.skip(f"pid {DEAD_PID} is live on this host")


def _seed(tmp_path, now):
    """The three-row shape the defect needs: a live service, a dead newer REPL,
    and a legacy row from before the pid field existed."""
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    # The service: live (this very process), started first.
    log.record("autonomy_started", user_id="", source="runtime",
               attrs={"loops": ["cron", "goals", "curator"],
                      "pid": os.getpid(), "role": "telegram"},
               ts=now - 600)
    # A legacy row — written before `pid` existed. Kept, never attributed.
    log.record("autonomy_started", user_id="", source="runtime",
               attrs={"loops": ["settlement"]}, ts=now - 300)
    # The REPL that has since exited: NEWEST, and narrower.
    log.record("autonomy_started", user_id="", source="runtime",
               attrs={"loops": ["goals", "repl_only"],
                      "pid": _dead_pid(), "role": "repl"},
               ts=now)
    return log


def _silent(snap):
    return {h.key.split(":", 1)[1] for h in snap.health
            if h.key.startswith("loop_silent:")}


def test_dead_repl_row_does_not_replace_the_service_loop_set(tmp_path):
    """The regression itself: before the fix, `expected` was the newest row's
    `["goals", "repl_only"]` and `curator` was never checked again."""
    from core.status_snapshot import build_status_snapshot
    _seed(tmp_path, time.time())
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    silent = _silent(snap)

    # The live service's own set survived a newer, narrower row.
    assert "curator" in silent, f"the dead REPL redefined the expected set: {silent}"
    assert "cron" in silent
    assert "goals" in silent
    # The legacy (pid-less) row is kept — it cannot be attributed, so dropping
    # it would narrow the set on no evidence.
    assert "settlement" in silent


def test_a_dead_process_only_loops_are_dropped(tmp_path):
    """The other half: a loop only a GONE process ever started is not expected
    of anybody, so reporting it silent would be noise about a process that is
    correctly not running."""
    from core.status_snapshot import build_status_snapshot
    _seed(tmp_path, time.time())
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert "repl_only" not in _silent(snap)


def test_a_live_process_row_survives_being_oldest(tmp_path):
    """Order does not decide membership — liveness does."""
    from core.status_snapshot import build_status_snapshot
    now = time.time()
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("autonomy_started", user_id="", source="runtime",
               attrs={"loops": ["bridges"], "pid": os.getpid(), "role": "api"},
               ts=now - 10_000)
    for i in range(5):
        log.record("autonomy_started", user_id="", source="runtime",
                   attrs={"loops": ["goals"], "pid": _dead_pid(), "role": "repl"},
                   ts=now - i)
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert "bridges" in _silent(snap)


def test_many_short_repl_rows_cannot_evict_the_live_service_row(tmp_path):
    """The read window is per-PROCESS, not per-row and not per-day.

    ⚠️ Both of the obvious bounds lose a live process. A count window (this was
    `LIMIT 20`) is evicted by process STARTS — 25 short `rob` runs on a local
    box push the service's own row out and `expected` silently narrows back to
    gate-derived. A time floor is worse: one row is written per process start,
    so a service up longer than the window (prod's normal state) falls out of
    it, which is the horizon the read's own comment forbids.
    """
    from core.status_snapshot import build_status_snapshot
    now = time.time()
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    # The live service: OLDEST row in the store, and up for six weeks — longer
    # than any time window anybody would pick.
    log.record("autonomy_started", user_id="", source="runtime",
               attrs={"loops": ["cron", "goals", "curator", "settlement"],
                      "pid": os.getpid(), "role": "telegram"},
               ts=now - 42 * 86400)
    dead = _dead_pid()
    for i in range(25):
        log.record("autonomy_started", user_id="", source="runtime",
                   attrs={"loops": ["goals"], "pid": dead + i, "role": "repl"},
                   ts=now - (25 - i))
    silent = _silent(build_status_snapshot(
        "u1", data_dir=str(tmp_path), include_money=False))
    for loop in ("cron", "goals", "curator", "settlement"):
        assert loop in silent, f"the service's {loop} fell out of the window: {silent}"


def test_newest_row_per_process_wins_within_a_group(tmp_path):
    """A restarted service writes a SECOND row under a new pid set; the one that
    describes the process as it runs now is its newest."""
    from core.status_snapshot import build_status_snapshot
    now = time.time()
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("autonomy_started", user_id="", source="runtime",
               attrs={"loops": ["curator"], "pid": os.getpid(), "role": "telegram"},
               ts=now - 900)
    log.record("autonomy_started", user_id="", source="runtime",
               attrs={"loops": ["bridges"], "pid": os.getpid(), "role": "telegram"},
               ts=now - 10)
    silent = _silent(snap := build_status_snapshot(
        "u1", data_dir=str(tmp_path), include_money=False))
    assert "bridges" in silent
    assert "curator" not in silent, f"a superseded row for the same pid: {silent}"
    assert snap is not None


def test_no_started_row_falls_back_to_the_gate_pair(tmp_path, monkeypatch):
    """An install with no record at all keeps the legacy gate-derived set."""
    from core.status_snapshot import build_status_snapshot
    monkeypatch.setenv("CRON_ENABLED", "true")
    monkeypatch.setenv("GOALS_ENABLED", "true")
    TelemetryEventLog(str(tmp_path / "telemetry_events.db")).record(
        "autonomy_tick", user_id="", source="runtime",
        attrs={"loop": "cron", "alive": True})
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert "goals" in _silent(snap)


def test_malformed_attrs_are_counted_not_dropped(tmp_path):
    """A row whose attrs cannot be parsed must not be fatal — and must not be
    silent either. It is COUNTED and the caller raises a health item, because
    a row nobody could read is a process whose loops nobody is checking."""
    import core.status_snapshot as ss
    rows = [{"ts": 4.0, "attrs": "{not json"},
            {"ts": 3.0, "attrs": '["a", "list"]'},
            {"ts": 2.0, "attrs": None},
            {"ts": 1.0, "attrs": '{"loops": ["cron"], "pid": %d}' % os.getpid()}]
    loops, unreadable = ss._live_started_loops(rows)
    assert loops == ["cron"]
    assert unreadable == 2, "the unparseable and the non-object row both count"


def test_unparseable_started_row_raises_a_health_item(tmp_path):
    """The counted rows reach the owner, never a quiet narrower set."""
    import sqlite3

    from core.status_snapshot import build_status_snapshot
    db = str(tmp_path / "telemetry_events.db")
    log = TelemetryEventLog(db)
    log.record("autonomy_started", user_id="", source="runtime",
               attrs={"loops": ["cron"], "pid": os.getpid()})
    con = sqlite3.connect(db)
    con.execute("UPDATE telemetry_events SET attrs=? WHERE kind=?",
                ("{not json", "autonomy_started"))
    con.commit()
    con.close()
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert "loops_expected_partial" in {h.key for h in snap.health}


def test_one_corrupt_row_does_not_take_out_the_whole_read(tmp_path):
    """⚠️ The read collapses rows by pid in SQL, and `json_extract` RAISES on
    malformed JSON — so a GROUP BY that touched every row would let ONE corrupt
    row fail the entire query and narrow `expected` to gate-derived. Only
    pid-BEARING rows are collapsed; everything else passes through to Python,
    which counts it. The live row's loops survive beside the warning."""
    import sqlite3

    from core.status_snapshot import build_status_snapshot
    db = str(tmp_path / "telemetry_events.db")
    log = TelemetryEventLog(db)
    now = time.time()
    log.record("autonomy_started", user_id="", source="runtime",
               attrs={"loops": ["curator", "settlement"], "pid": os.getpid()},
               ts=now - 100)
    log.record("autonomy_started", user_id="", source="runtime",
               attrs={"loops": ["ignored"]}, ts=now)
    con = sqlite3.connect(db)
    con.execute("UPDATE telemetry_events SET attrs='{not json' "
                "WHERE kind=? AND ts > ?", ("autonomy_started", now - 1))
    con.commit()
    con.close()

    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    silent = _silent(snap)
    assert "curator" in silent, f"a corrupt row took out the live row: {silent}"
    assert "settlement" in silent
    assert "loops_expected_partial" in {h.key for h in snap.health}
    assert "loops_expected_unreadable" not in {h.key for h in snap.health}


def test_pid_alive_fails_open_on_an_unknown_answer(monkeypatch):
    """Only `ProcessLookupError` means gone. A permission error means alive
    under another uid, and anything else is unknown — both keep the row,
    because narrowing on a guess is the failure this whole change is about."""
    import core.status_snapshot as ss

    def _raise(exc):
        def _k(pid, sig):
            raise exc
        return _k

    monkeypatch.setattr(os, "kill", _raise(PermissionError()))
    assert ss._pid_alive(1234) is True
    monkeypatch.setattr(os, "kill", _raise(OSError("weird platform")))
    assert ss._pid_alive(1234) is True
    monkeypatch.setattr(os, "kill", _raise(ProcessLookupError()))
    assert ss._pid_alive(1234) is False


def test_non_positive_pid_is_treated_as_legacy(tmp_path):
    """`os.kill(0, 0)` addresses the whole process GROUP — a pid that is not a
    positive int is never probed; it is an unattributable row, like a legacy one.
    """
    import core.status_snapshot as ss
    rows = [{"ts": 2.0, "attrs": '{"loops": ["cron"], "pid": 0}'},
            {"ts": 1.0, "attrs": '{"loops": ["goals"], "pid": "not-a-pid"}'}]
    # Both collapse into the single anonymous group, whose NEWEST row wins.
    assert ss._live_started_loops(rows) == (["cron"], 0)


# ==========================================================================
# The writer half
# ==========================================================================

@pytest.mark.asyncio
async def test_start_autonomy_stamps_pid_and_role(monkeypatch):
    """Without the pid the reader has nothing to attribute a row to."""
    import core.autonomy_runtime as ar

    class _FakeTicker:
        async def run_forever(self, *, stop_event):
            await stop_event.wait()

    monkeypatch.setattr(ar, "_build_cron_ticker", lambda ta, data_dir: _FakeTicker())
    monkeypatch.setattr(ar, "_build_goal_ticker", lambda ta, data_dir: _FakeTicker())
    monkeypatch.setattr(ar, "_cron_enabled", lambda: True)
    monkeypatch.setattr(ar, "_goals_enabled", lambda: True)
    monkeypatch.setattr(ar, "_curator_enabled", lambda: False)
    monkeypatch.setattr(ar, "_surface_gc_enabled", lambda: False)
    monkeypatch.setattr(ar, "_quiet_release_enabled", lambda: False)
    monkeypatch.setattr(ar, "_sandbox_reap_enabled", lambda: False)

    handles = ar.start_autonomy(task_agent=object(), data_dir="data")
    try:
        from core.event_log import get_event_log
        rows = get_event_log().query(kind="autonomy_started")
        assert rows, "no autonomy_started row recorded"
        attrs = rows[0]["attrs"]
        assert attrs["pid"] == os.getpid()
        assert isinstance(attrs.get("role"), str) and attrs["role"]
        assert sorted(attrs["loops"]) == ["cron", "goals"]
    finally:
        await handles.stop()


def test_service_role_is_derived_from_argv_not_an_env_var(monkeypatch):
    """The role is an OBSERVATION about the running process, never a knob: an
    env var would need a catalog row (`test_flags_reverse`) and would be one
    more thing an operator could set to something untrue."""
    import sys

    import core.autonomy_runtime as ar

    monkeypatch.setattr(sys, "argv", ["/opt/polyrob/venv/bin/polyrob", "telegram"])
    assert ar._service_role() == "telegram"
    monkeypatch.setattr(sys, "argv", ["/opt/polyrob/venv/bin/polyrob", "email"])
    assert ar._service_role() == "email"
    monkeypatch.setattr(sys, "argv", ["/opt/polyrob/venv/bin/polyrob"])
    assert ar._service_role() == "repl"
    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert ar._service_role() == "api"
    monkeypatch.setattr(sys, "argv", [])
    assert ar._service_role() == "unknown"
    # A free-text argument is never mistaken for a role.
    monkeypatch.setattr(sys, "argv", ["polyrob", "run", "post this to telegram"])
    assert ar._service_role() == "run"


def test_a_group_options_value_is_not_the_subcommand(monkeypatch):
    """⚠️ `-P work telegram` used to report the role `work` — a PROFILE NAME in
    a field that promises a process role. The value-taking group options live in
    `cli/polyrob.py::cli`; their argument is the next token."""
    import sys

    import core.autonomy_runtime as ar

    for opt in ("-P", "--profile"):
        monkeypatch.setattr(sys, "argv", ["polyrob", opt, "work", "telegram"])
        assert ar._service_role() == "telegram", opt
    for opt in ("--project", "--model", "-m", "--provider", "-p", "--toolset"):
        monkeypatch.setattr(sys, "argv", ["polyrob", opt, "somevalue", "email"])
        assert ar._service_role() == "email", opt
    # The `--opt=value` form is ONE token: nothing extra may be skipped, or the
    # subcommand right after it would be eaten.
    monkeypatch.setattr(sys, "argv", ["polyrob", "--profile=work", "telegram"])
    assert ar._service_role() == "telegram"
    # A valueless flag still skips only itself.
    monkeypatch.setattr(sys, "argv", ["polyrob", "--plain", "telegram"])
    assert ar._service_role() == "telegram"
    # Stacked: a flag, then an option with a value, then the subcommand.
    monkeypatch.setattr(sys, "argv",
                        ["polyrob", "--plain", "-P", "work", "-m", "glm-5", "email"])
    assert ar._service_role() == "email"
    # A profile run with NO subcommand is still the REPL, not the profile name.
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/polyrob", "-P", "work"])
    assert ar._service_role() == "repl"


def test_uvicorn_console_and_api_are_named_apart(monkeypatch):
    """`python -m uvicorn` puts the PACKAGE's `__main__.py` in argv[0], so the
    basename is not "uvicorn" — the whole path is what identifies it. The
    console and the API service are two processes with two loop sets, so they
    get two names."""
    import sys

    import core.autonomy_runtime as ar

    uvicorn_m = "/opt/polyrob/venv/lib/python3.11/site-packages/uvicorn/__main__.py"
    monkeypatch.setattr(sys, "argv", [
        uvicorn_m, "webview.server:app", "--host", "127.0.0.1", "--port", "5050"])
    assert ar._service_role() == "console"
    # The console script form resolves the same way.
    monkeypatch.setattr(sys, "argv", ["/opt/polyrob/venv/bin/uvicorn", "webview.server:app"])
    assert ar._service_role() == "console"
    # Any other ASGI app served by uvicorn is the API, not the console.
    monkeypatch.setattr(sys, "argv", [uvicorn_m, "api.app:app", "--port", "9000"])
    assert ar._service_role() == "api"
    # Leading flags do not hide the app target.
    monkeypatch.setattr(sys, "argv", [uvicorn_m, "--reload", "webview.server:app"])
    assert ar._service_role() == "console"
    # uvicorn with no app named at all is still not "unknown" — it is a server.
    monkeypatch.setattr(sys, "argv", [uvicorn_m])
    assert ar._service_role() == "api"


def test_service_role_never_raises(monkeypatch):
    import sys

    import core.autonomy_runtime as ar

    monkeypatch.setattr(sys, "argv", [None, object()])
    assert isinstance(ar._service_role(), str)
