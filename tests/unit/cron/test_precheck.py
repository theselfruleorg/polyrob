"""2026-10-14 review A-12 — a watch job's $0 pre-check.

Watch jobs (monitor/scout/exit/watcher) were ≈ 36 % of 30-day spend and mostly
concluded "nothing new" (EXIT MONITOR 24 runs, 0 useful). A job may declare
`payload.precheck` — a 071 read verb run in-process before the model — and a
quiet read is a `cron_run skipped/precheck_quiet` with no session at all.
"""
from types import SimpleNamespace

import pytest

from cron import read_job as P
from cron.jobs import CronJob, CronJobStore
from cron.schedule import ScheduleError
from cron.service import CronService

TKN = "0x5555555555555555555555555555555555555555"


def _spec(**kw):
    base = {"verb": "defi_data.price", "params": {"chain": "base", "address": TKN}}
    base.update(kw)
    return base


def _job(payload, jid="w1"):
    return CronJob(id=jid, task="watch TKN and act on a move", schedule_spec="30m",
                   user_id="rob", next_run_at=None, payload=payload,
                   max_duration_seconds=600)


def _tool(value=1.0, error=None):
    class _P:
        async def price(self, params, *a):
            return SimpleNamespace(error=error, extracted_content=f"price {value}",
                                   metadata={"usd": value})
    return _P


# -- validation ---------------------------------------------------------------

def test_a_precheck_that_can_never_say_quiet_is_refused():
    with pytest.raises(P.PrecheckError):
        P.validate_precheck(_spec())                       # no alert, no move_pct
    with pytest.raises(P.PrecheckError):
        P.validate_precheck(_spec(move_pct=5))              # move_pct without field
    with pytest.raises(P.PrecheckError):
        P.validate_precheck(_spec(verb="defi_trade.swap", move_pct=5, field="usd"))
    with pytest.raises(P.PrecheckError):
        P.validate_precheck(_spec(move_pct=0, field="usd"))
    ok = P.validate_precheck(_spec(move_pct="5", field="usd", max_quiet_hours=24))
    assert ok["move_pct"] == 5.0 and ok["field"] == "usd" and ok["max_quiet_hours"] == 24.0


def test_service_validates_and_refuses_a_precheck_on_a_model_free_job(tmp_path):
    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    job = svc.schedule(task="watch the TKN pool for a move", schedule_spec="30m",
                       user_id="rob", payload={"precheck": _spec(move_pct=5, field="usd")})
    assert job.payload["precheck"]["move_pct"] == 5.0
    with pytest.raises(ScheduleError):
        svc.schedule(task="watch the TKN pool for a move", schedule_spec="30m", user_id="rob",
                     payload={"precheck": _spec()})
    with pytest.raises(ScheduleError):
        svc.schedule(task="read the TKN pool every tick", schedule_spec="30m", user_id="rob",
                     payload={"precheck": _spec(move_pct=5, field="usd"),
                              "read_verb": {"verb": "defi_data.price",
                                            "params": {"chain": "base", "address": TKN}}})
    changed = svc.edit(job.id, user_id="rob",
                       payload_updates={"precheck": _spec(alert={"field": "usd", "below": 0.5})})
    assert changed


# -- the verdict --------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_precheck_is_none():
    assert await P.run_precheck(_job({}), data_dir="/nonexistent") is None


@pytest.mark.asyncio
async def test_alert_mode_quiet_and_fired():
    spec = _spec(alert={"field": "usd", "below": 0.5})
    quiet = await P.run_precheck(_job({"precheck": spec}), data_dir="x", tool_factory=_tool(1.0))
    assert quiet.skip and quiet.reason == P.QUIET_REASON
    fired = await P.run_precheck(_job({"precheck": spec}), data_dir="x", tool_factory=_tool(0.2))
    assert not fired.skip and fired.reason == "precheck_alert"
    assert "<precheck" in P.context_block(fired) and "ALERT" in P.context_block(fired)
    assert P.context_block(quiet) == ""


@pytest.mark.asyncio
async def test_a_read_error_or_unreadable_value_runs_the_model(tmp_path):
    spec = _spec(move_pct=5, field="usd")
    err = await P.run_precheck(_job({"precheck": spec}), data_dir=str(tmp_path),
                               tool_factory=_tool(error="rpc down"))
    assert not err.skip and err.reason == "precheck_error"
    unread = await P.run_precheck(_job({"precheck": spec}), data_dir=str(tmp_path),
                                  tool_factory=_tool(value=None))
    assert not unread.skip and "UNKNOWN" in unread.context


@pytest.mark.asyncio
async def test_move_mode_needs_a_baseline_then_skips_small_moves(tmp_path):
    spec = _spec(move_pct=5, field="usd")
    job = _job({"precheck": spec})
    first = await P.run_precheck(job, data_dir=str(tmp_path), tool_factory=_tool(1.0))
    assert not first.skip and first.reason == "precheck_no_baseline"
    P.record_baseline(job, first.value, data_dir=str(tmp_path))
    small = await P.run_precheck(job, data_dir=str(tmp_path), tool_factory=_tool(1.03))
    assert small.skip and small.attrs["baseline"] == 1.0
    big = await P.run_precheck(job, data_dir=str(tmp_path), tool_factory=_tool(0.9))
    assert not big.skip and big.reason == "precheck_moved"


@pytest.mark.asyncio
async def test_max_quiet_hours_runs_anyway(tmp_path, monkeypatch):
    spec = _spec(move_pct=5, field="usd", max_quiet_hours=1)
    job = _job({"precheck": spec})
    P.record_baseline(job, 1.0, data_dir=str(tmp_path))
    real = P.time.time
    monkeypatch.setattr(P.time, "time", lambda: real() + 7200)
    late = await P.run_precheck(job, data_dir=str(tmp_path), tool_factory=_tool(1.0))
    assert not late.skip and late.reason == "precheck_quiet_too_long"


# -- the runner ---------------------------------------------------------------

@pytest.fixture
def log(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    test_log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: test_log)
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)
    return test_log


class _Agent:
    async def create_session(self, *, user_id, request):
        raise AssertionError("a quiet pre-check must not start a session")


@pytest.mark.asyncio
async def test_runner_quiet_precheck_is_a_zero_dollar_tick(tmp_path, monkeypatch, log):
    from cron.runner import make_agent_runner
    monkeypatch.setenv("CRON_RUN_LOOP", "true")

    async def quiet(job, *, data_dir, tool_factory=None):
        return P.PrecheckVerdict(True, P.QUIET_REASON, {"precheck": "defi_data.price",
                                                        "value": 1.0})
    monkeypatch.setattr(P, "run_precheck", quiet)
    ok = await make_agent_runner(_Agent(), data_dir=str(tmp_path))(
        _job({"precheck": _spec(move_pct=5, field="usd")}))
    assert ok is True
    rows = log.query(kind="cron_run")
    assert [r["attrs"]["reason"] for r in rows] == [P.QUIET_REASON]


@pytest.mark.asyncio
async def test_runner_fired_precheck_rides_into_the_task_and_sets_baseline(
        tmp_path, monkeypatch, log):
    from agents.task.runtime.run_outcome import RunOutcome
    from cron.runner import make_agent_runner
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    seen = {}

    async def moved(job, *, data_dir, tool_factory=None):
        return P.PrecheckVerdict(False, "precheck_moved", {}, "price 0.9 (was 1.0)", 0.9)
    monkeypatch.setattr(P, "run_precheck", moved)

    async def fake_run(task_agent, *, user_id, request, **k):
        seen["task"] = request["task"]
        return RunOutcome(session_id="s1", status="ok", done_called=True, done_text="x")
    monkeypatch.setattr("cron.runner._run_task_to_outcome", fake_run)
    job = _job({"precheck": _spec(move_pct=5, field="usd")})
    assert await make_agent_runner(_Agent(), data_dir=str(tmp_path))(job) is True
    assert seen["task"].startswith('<precheck reason="precheck_moved">')
    assert P._baseline("w1", str(tmp_path))[0] == 0.9
