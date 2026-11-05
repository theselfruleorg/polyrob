"""Autonomous work waits under memory or disk pressure; an owner turn never does.

Nothing refused a run before a peak: the only memory check logged, and it compared the
size of the message history. A hold is not a failure, and an unreadable source allows
the run.
"""
import asyncio
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.memory_pressure as mp
from core.event_kinds import RESOURCE_HOLD

MB = 1024 * 1024
GB = 1024 * MB
_Usage = namedtuple("usage", "total used free")


def _roots(tmp_path, *, available_kb=4_000_000, cgroup="0::/system.slice/agent.service\n",
           current=None, high=None):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    if available_kb is not None:
        (proc / "meminfo").write_text(
            f"MemTotal:       8000000 kB\nMemFree:         100000 kB\n"
            f"MemAvailable:   {available_kb} kB\n")
    if cgroup is not None:
        (proc / "self" / "cgroup").write_text(cgroup)
    root = tmp_path / "cgroup"
    unit = root / "system.slice" / "agent.service"
    unit.mkdir(parents=True)
    if current is not None:
        (unit / "memory.current").write_text(f"{current}\n")
    if high is not None:
        (unit / "memory.high").write_text(f"{high}\n")
    return str(proc), str(root)


@pytest.fixture
def plenty_of_disk(monkeypatch):
    monkeypatch.setattr(mp.shutil, "disk_usage", lambda p: _Usage(100 * GB, 50 * GB, 50 * GB))


def _read(tmp_path, **kw):
    proc, root = _roots(tmp_path, **kw)
    return mp.read_pressure(str(tmp_path), proc_root=proc, cgroup_root=root)


def test_the_thresholds_are_the_agreed_constants():
    assert mp.MEM_AVAILABLE_FLOOR_BYTES == 500 * MB
    assert mp.DISK_FREE_FLOOR_BYTES == 2 * GB
    assert (mp.CGROUP_HIGH_NUM, mp.CGROUP_HIGH_DEN) == (9, 10)


def test_no_pressure_holds_nothing(tmp_path, plenty_of_disk):
    p = _read(tmp_path, current=100 * MB, high=2400 * MB)
    assert p.hold is False
    assert p.unread == ()
    assert p.mem_available_mb > 3000


def test_low_mem_available_holds(tmp_path, plenty_of_disk):
    p = _read(tmp_path, available_kb=400 * 1024)
    assert p.hold is True
    assert p.keys == ("mem_available",)
    assert "400 MB" in p.reasons[0]


def test_mem_available_exactly_at_the_floor_does_not_hold(tmp_path, plenty_of_disk):
    assert _read(tmp_path, available_kb=500 * 1024).hold is False


def test_a_small_machine_is_not_held_for_ever(tmp_path, plenty_of_disk):
    """A 1 GB box idles under 500 MB: a fixed floor would stop all goals and cron."""
    proc, root = _roots(tmp_path, available_kb=300 * 1024)
    (Path(proc) / "meminfo").write_text("MemTotal: 1048576 kB\nMemAvailable: 307200 kB\n")
    assert mp.read_pressure(str(tmp_path), proc_root=proc, cgroup_root=root).hold is False
    (Path(proc) / "meminfo").write_text("MemTotal: 1048576 kB\nMemAvailable: 51200 kB\n")
    assert mp.read_pressure(str(tmp_path), proc_root=proc, cgroup_root=root).keys == ("mem_available",)


def test_a_small_disk_uses_a_share_of_its_size(tmp_path, monkeypatch):
    monkeypatch.setattr(mp.shutil, "disk_usage", lambda p: _Usage(10 * GB, 9 * GB, GB))
    assert _read(tmp_path / "a").hold is False
    monkeypatch.setattr(mp.shutil, "disk_usage", lambda p: _Usage(10 * GB, 9900 * MB, 100 * MB))
    assert _read(tmp_path / "b").keys == ("disk_free",)


def test_cgroup_at_ninety_percent_of_high_holds(tmp_path, plenty_of_disk):
    p = _read(tmp_path, current=900 * MB, high=1000 * MB)
    assert p.keys == ("cgroup_high",)


def test_cgroup_page_cache_is_not_pressure(tmp_path, plenty_of_disk):
    proc, root = _roots(tmp_path, current=950 * MB, high=1000 * MB)
    stat = tmp_path / "cgroup" / "system.slice" / "agent.service" / "memory.stat"
    stat.write_text(f"anon {300 * MB}\ninactive_file {500 * MB}\n")
    p = mp.read_pressure(str(tmp_path), proc_root=proc, cgroup_root=root)
    assert not p.hold and p.cgroup_current_mb == 450


def test_cgroup_under_ninety_percent_does_not_hold(tmp_path, plenty_of_disk):
    assert _read(tmp_path, current=900 * MB - 1, high=1000 * MB).hold is False


def test_memory_high_max_means_the_check_does_not_apply(tmp_path, plenty_of_disk):
    p = _read(tmp_path, current=9999 * MB, high="max")
    assert p.hold is False
    assert p.unread == ()


def test_memory_high_absent_means_the_check_does_not_apply(tmp_path, plenty_of_disk):
    p = _read(tmp_path, current=9999 * MB, high=None)
    assert p.hold is False
    assert p.unread == ()


def test_no_cgroup_v2_line_means_the_check_does_not_apply(tmp_path, plenty_of_disk):
    p = _read(tmp_path, cgroup="4:memory:/legacy\n", current=9999 * MB, high=100 * MB)
    assert p.hold is False
    assert p.unread == ()


def test_low_disk_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(mp.shutil, "disk_usage", lambda p: _Usage(100 * GB, 99 * GB, GB))
    p = _read(tmp_path)
    assert p.keys == ("disk_free",)
    assert "1024 MB" in p.reasons[0]


def test_disk_exactly_at_the_floor_does_not_hold(tmp_path, monkeypatch):
    monkeypatch.setattr(mp.shutil, "disk_usage", lambda p: _Usage(100 * GB, 98 * GB, 2 * GB))
    assert _read(tmp_path).hold is False


def test_every_unreadable_source_fails_open_and_is_named(tmp_path, monkeypatch):
    def _no_disk(path):
        raise OSError("gone")

    monkeypatch.setattr(mp.shutil, "disk_usage", _no_disk)
    p = _read(tmp_path, available_kb=None, current=None, high=1000 * MB)
    assert p.hold is False
    assert set(p.unread) == {"MemAvailable", "memory.current", "free disk"}


def test_garbage_in_a_source_fails_open_and_is_named(tmp_path, plenty_of_disk):
    p = _read(tmp_path, current="not-a-number", high=1000 * MB)
    assert p.hold is False
    assert p.unread == ("memory.current",)


def test_a_missing_meminfo_line_is_unread_not_zero(tmp_path, plenty_of_disk):
    proc, root = _roots(tmp_path)
    Path(proc, "meminfo").write_text("MemTotal: 1 kB\n")
    p = mp.read_pressure(str(tmp_path), proc_root=proc, cgroup_root=root)
    assert p.hold is False
    assert p.unread == ("MemAvailable",)


def test_the_module_defaults_are_read_at_call_time(tmp_path, monkeypatch, plenty_of_disk):
    proc, root = _roots(tmp_path, available_kb=1)
    monkeypatch.setattr(mp, "PROC_ROOT", proc)
    monkeypatch.setattr(mp, "CGROUP_ROOT", root)
    assert mp.current_hold(str(tmp_path)) is not None


@pytest.fixture
def evlog(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    monkeypatch.setattr(mp, "_last_noted", {})
    return log


@pytest.fixture
def low_memory(tmp_path, monkeypatch, plenty_of_disk):
    proc, root = _roots(tmp_path, available_kb=1024)
    monkeypatch.setattr(mp, "PROC_ROOT", proc)
    monkeypatch.setattr(mp, "CGROUP_ROOT", root)


def test_a_hold_event_fires_on_the_edge_and_again_after_an_hour(evlog, low_memory, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(mp.time, "monotonic", lambda: clock[0])
    p = mp.current_hold(".")
    mp.note_hold("cron", p, waiting=2)
    mp.note_hold("cron", p, waiting=2)
    assert len(evlog.query(kind=RESOURCE_HOLD)) == 1
    mp.note_hold("goals", p, waiting=1)
    assert len(evlog.query(kind=RESOURCE_HOLD)) == 2, "each runner has its own edge"
    clock[0] += mp.HOLD_RENOTE_S + 1
    mp.note_hold("cron", p, waiting=2)
    rows = evlog.query(kind=RESOURCE_HOLD)
    assert len(rows) == 3
    assert rows[0]["attrs"]["runner"] == "cron"
    assert rows[0]["attrs"]["keys"] == ["mem_available"]
    assert rows[0]["attrs"]["waiting"] == 2


@pytest.mark.asyncio
async def test_a_held_goal_is_not_claimed(tmp_path, monkeypatch, evlog, low_memory):
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.dispatcher import GoalDispatcher
    monkeypatch.setenv("GOALS_ENABLED", "true")
    board = GoalBoard(str(tmp_path / "goals.db"))
    goal = board.create(user_id="alice", title="Round 15", priority=5)
    disp = GoalDispatcher(board, None, lock_path=str(tmp_path / "t.lock"))

    assert await disp.dispatch_once() == 0

    after = board.get(goal.id)
    assert after.status == "ready"
    assert board.count_running() == 0
    rows = evlog.query(kind=RESOURCE_HOLD)
    assert [r["attrs"]["runner"] for r in rows] == ["goals"]
    assert rows[0]["attrs"]["waiting"] == 1


@pytest.mark.asyncio
async def test_an_empty_board_under_pressure_claims_to_hold_nothing(tmp_path, monkeypatch, evlog,
                                                                    low_memory):
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.dispatcher import GoalDispatcher
    monkeypatch.setenv("GOALS_ENABLED", "true")
    disp = GoalDispatcher(GoalBoard(str(tmp_path / "goals.db")), None,
                          lock_path=str(tmp_path / "t.lock"))
    assert await disp.dispatch_once() == 0
    assert evlog.query(kind=RESOURCE_HOLD) == []


def _cron(tmp_path, runner):
    from cron.jobs import CronJob, CronJobStore
    from cron.scheduler import CronScheduler
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(CronJob(id="job1", task="Report", schedule_spec="1h", user_id="u1",
                      next_run_at=datetime(2026, 1, 1, 10, 0)))
    return store, CronScheduler(store, runner, lock_path=str(tmp_path / "lock"))


def test_a_held_cron_job_stays_due_with_no_failure_and_no_retry_count(tmp_path, evlog, low_memory):
    ran = []

    async def runner(job):
        ran.append(job.id)
        return True

    store, sched = _cron(tmp_path, runner)
    before = store.get("job1")
    now = datetime(2026, 1, 1, 10, 1)
    result = asyncio.run(sched.tick(now))

    assert ran == []
    assert (result.ran, result.failed, result.deferred) == ([], [], [])
    assert result.skipped_pressure is True
    after = store.get("job1")
    assert after.status == "scheduled"
    assert after.next_run_at == before.next_run_at
    assert after.payload == before.payload
    assert after.last_run_at == before.last_run_at
    assert [j.id for j in store.due(now)] == ["job1"]
    assert evlog.query(kind=RESOURCE_HOLD)[0]["attrs"]["runner"] == "cron"


def test_a_held_cron_tick_does_not_preempt_a_goal(tmp_path, evlog, low_memory, monkeypatch):
    async def runner(job):
        return True

    _store, sched = _cron(tmp_path, runner)
    called = []

    async def _yield(now):
        called.append(now)
        return ["goal-1"]

    monkeypatch.setattr(sched, "_maybe_yield_for_money", _yield)
    import core.interactive_gate as gate
    monkeypatch.setattr(gate, "is_interactive_busy", lambda: True)
    result = asyncio.run(sched.tick(datetime(2026, 1, 1, 10, 1)))
    assert called == [], "a job that cannot run must not cancel a goal for its slot"
    assert result.yielded == []


def test_an_unreadable_source_allows_the_cron_run(tmp_path, evlog, monkeypatch, plenty_of_disk):
    monkeypatch.setattr(mp, "PROC_ROOT", str(tmp_path / "nothing"))
    monkeypatch.setattr(mp, "CGROUP_ROOT", str(tmp_path / "nothing"))
    ran = []

    async def runner(job):
        ran.append(job.id)
        return True

    _store, sched = _cron(tmp_path, runner)
    asyncio.run(sched.tick(datetime(2026, 1, 1, 10, 1)))
    assert ran == ["job1"]
    assert evlog.query(kind=RESOURCE_HOLD) == []


def test_no_owner_turn_path_imports_the_gate():
    repo = Path(__file__).resolve().parents[3]
    users = []
    for top in ("agents", "api", "cli", "core", "cron", "surfaces", "tools", "webview"):
        for py in (repo / top).rglob("*.py"):
            if py.name == "memory_pressure.py":
                continue
            src = py.read_text(encoding="utf-8", errors="replace")
            if "current_hold" in src:
                users.append(str(py.relative_to(repo)))
    assert sorted(users) == ["agents/task/goals/dispatcher.py", "cron/scheduler.py"]


def _section(tele=None):
    from core.status_loops import _resource_pressure_lines
    from core.status_types import Section
    sec = Section(name="loops")
    _resource_pressure_lines(sec, tele or Section(name="telemetry", state="unavailable", reason="x"),
                             2000.0, "/tmp")
    return sec


def test_status_says_could_not_read(monkeypatch):
    monkeypatch.setattr(mp, "read_pressure", lambda _disk: mp.Pressure(unread=("MemAvailable",)))
    monkeypatch.setattr("core.status_loops.sys.platform", "linux")  # /proc is Linux only
    sec = _section()
    assert sec.lines == ["resource pressure: could not read MemAvailable"]
    assert sec.health == []
    assert sec.data["resource_pressure"]["unread"] == ["MemAvailable"]


def test_status_warns_while_holding(monkeypatch):
    held = mp.Pressure(keys=("disk_free",), reasons=("only 1024 MB of disk free",))
    monkeypatch.setattr(mp, "read_pressure", lambda _disk: held)
    sec = _section()
    assert "new goals and cron jobs wait" in sec.lines[0]
    assert "only 1024 MB of disk free" in sec.lines[0]
    assert [h.key for h in sec.health] == ["resource_hold"]


def test_status_names_a_clear_read(monkeypatch):
    monkeypatch.setattr(mp, "read_pressure", lambda _disk: mp.Pressure())
    assert _section().lines == ["resource pressure: none"]


def test_status_names_the_last_hold_of_the_window(monkeypatch):
    from core.status_types import Section
    monkeypatch.setattr(mp, "read_pressure", lambda _disk: mp.Pressure())
    tele = Section(name="telemetry", data={"rows": [
        {"kind": RESOURCE_HOLD, "ts": 1900.0, "attrs": {"runner": "cron", "keys": ["disk_free"]}},
        {"kind": RESOURCE_HOLD, "ts": 1800.0, "attrs": {"runner": "goals", "keys": ["disk_free"]}},
    ]})
    line = _section(tele).lines[1]
    assert "2 hold events" in line and "cron" in line and "disk_free" in line


def test_loops_section_always_renders_the_line(tmp_path, monkeypatch):
    from core.status_snapshot import _loops_section
    from core.status_types import Section
    monkeypatch.setattr(mp, "read_pressure", lambda _disk: mp.Pressure(unread=("free disk",)))
    sec = _loops_section("u1", str(tmp_path / "absent.db"),
                         Section(name="telemetry", state="unavailable", reason="x"), 2000.0)
    assert "resource pressure: could not read free disk" in sec.lines


def test_the_step_check_compares_rss_and_warns(caplog):
    import logging
    from agents.task.agent.core.step import _log_memory_use
    log = logging.getLogger("step-test")
    with caplog.at_level(logging.INFO, logger="step-test"):
        _log_memory_use(log, rss_mb=1200.0, history_mb=2.0, step=10)
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "RSS 1200 MB" in caplog.text


def test_a_large_history_with_a_small_rss_does_not_read_as_high_rss(caplog):
    import logging
    from agents.task.agent.core.step import _log_memory_use
    log = logging.getLogger("step-test")
    with caplog.at_level(logging.INFO, logger="step-test"):
        _log_memory_use(log, rss_mb=300.0, history_mb=60.0, step=10)
    assert "Message history using 60.0MB" in caplog.text
    assert "RSS" not in caplog.text


@pytest.mark.asyncio
async def test_a_long_hold_tells_the_owner_once(tmp_path, monkeypatch):
    sent = []

    async def _notify(text):
        sent.append(text)
    monkeypatch.setattr(mp, "_owner_notifier", _notify)
    monkeypatch.setattr(mp, "_owner_told", False)
    monkeypatch.setattr(mp, "_hold_started", {})
    monkeypatch.setattr(mp, "emit", lambda *a, **k: None)
    pressure = mp.Pressure(keys=("disk_free",), reasons=("only 1 MB of disk free",))
    clock = [1000.0]
    # mp.time, not time.monotonic: the event loop reads the real clock.
    monkeypatch.setattr(mp, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    mp.note_hold("goals", pressure, waiting=2)
    await asyncio.sleep(0)
    assert sent == [], "a short hold stays in /status"
    clock[0] += mp.HOLD_NOTICE_AFTER_S + 1
    mp.note_hold("goals", pressure, waiting=2)
    mp.note_hold("cron", pressure, waiting=1)
    await asyncio.sleep(0.01)
    assert len(sent) == 1 and "only 1 MB of disk free" in sent[0]
