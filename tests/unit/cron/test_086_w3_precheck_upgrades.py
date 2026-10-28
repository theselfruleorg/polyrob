"""086 W3: precheck list-alerts, string conditions, ``cancel_when`` (the job ends
itself, no model) and the job's ``payload.mandate`` riding into the verb."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from cron import read_job as P
from cron.jobs import CronJob, CronJobStore
from cron.schedule import ScheduleError
from cron.service import CronService

MEME = "0xb200000000000000000000ea8625786a776539fb"
EXIT_SPEC = {"verb": "defi_data.position_mark",
             "params": {"chain": "base", "account": "", "address": MEME},
             "alert": [{"field": "barrier", "not_equals": "none"}],
             "cancel_when": ["closed", "dust"]}


def _job(payload, jid="x1", user="rob"):
    return CronJob(id=jid, task="exit job", schedule_spec="15m", user_id=user,
                   next_run_at=None, payload=payload, max_duration_seconds=600)


def _mark_tool(meta, seen=None):
    class _T:
        async def position_mark(self, params, *a):
            if seen is not None:
                seen.append(params)
                from core.exec_identity import current_exec_identity
                seen.append(current_exec_identity()[0])
            return SimpleNamespace(error=None, extracted_content="mark", metadata=meta)
    return _T


# -- validation ---------------------------------------------------------------

def test_list_alert_and_string_ops_validate():
    out = P.validate_precheck(EXIT_SPEC)
    assert out["alert"] == [{"field": "barrier", "not_equals": "none"}]
    assert out["cancel_when"] == ["closed", "dust"]
    one = P.validate_precheck(dict(EXIT_SPEC, cancel_when="closed",
                                   alert={"field": "barrier", "equals": "crossed"}))
    assert one["cancel_when"] == ["closed"] and one["alert"]["equals"] == "crossed"


@pytest.mark.parametrize("bad", [
    {"cancel_when": ["gone"]}, {"cancel_when": []}, {"cancel_when": 3},
    {"alert": [{"field": "barrier"}]}, {"alert": [{"below": 1}]},
    {"alert": [{"field": "x", "equals": {"a": 1}}]},
    {"alert": [{"field": "x", "above": 1}] * 9},
])
def test_bad_shapes_are_refused(bad):
    with pytest.raises(P.PrecheckError):
        P.validate_precheck(dict(EXIT_SPEC, **bad))


def test_the_service_stores_the_exit_precheck(tmp_path):
    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    job = svc.schedule(task="exit job for MEME", schedule_spec="15m", user_id="rob",
                       payload={"precheck": EXIT_SPEC, "mandate": "m1"})
    assert job.payload["precheck"]["cancel_when"] == ["closed", "dust"]
    with pytest.raises(ScheduleError):
        svc.schedule(task="exit job for MEME", schedule_spec="15m", user_id="rob",
                     payload={"precheck": dict(EXIT_SPEC, cancel_when=["nope"])})


def test_alert_lines_list_any_crossing_and_unknown():
    conds = [{"field": "barrier", "not_equals": "none"}, {"field": "value_usd", "below": 1.0}]
    assert P.alert_lines(conds, {"barrier": "none", "value_usd": 5.0}) == []
    assert len(P.alert_lines(conds, {"barrier": "near", "value_usd": 5.0})) == 1
    assert len(P.alert_lines(conds, {"barrier": "crossed", "value_usd": 0.5})) == 2
    unknown = P.alert_lines(conds, {"value_usd": 5.0})
    assert len(unknown) == 1 and "UNKNOWN" in unknown[0]
    assert P.alert_lines({"field": "s", "equals": "x"}, {"s": "x"}) == ["ALERT: s = x."]


# -- the verdict --------------------------------------------------------------

@pytest.mark.asyncio
async def test_quiet_mark_is_a_zero_dollar_tick():
    v = await P.run_precheck(_job({"precheck": EXIT_SPEC}), data_dir="x",
                             tool_factory=_mark_tool({"barrier": "none", "closed": False,
                                                      "dust": False}))
    assert v.skip and not v.cancel


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier", ["near", "crossed", "unknown"])
async def test_any_barrier_state_but_none_wakes(barrier):
    v = await P.run_precheck(_job({"precheck": EXIT_SPEC}), data_dir="x",
                             tool_factory=_mark_tool({"barrier": barrier, "closed": False}))
    assert not v.skip and v.reason == "precheck_alert" and not v.cancel


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["closed", "dust"])
async def test_cancel_when_flag_cancels(flag):
    v = await P.run_precheck(_job({"precheck": EXIT_SPEC}), data_dir="x",
                             tool_factory=_mark_tool({"barrier": "none", flag: True}))
    assert v.cancel == flag and v.reason == P.ORPHAN_VIA and not v.skip


@pytest.mark.asyncio
async def test_a_failed_read_never_cancels():
    class _T:
        async def position_mark(self, params, *a):
            return SimpleNamespace(error="book unreadable", extracted_content="", metadata=None)
    v = await P.run_precheck(_job({"precheck": EXIT_SPEC}), data_dir="x", tool_factory=_T)
    assert not v.cancel and not v.skip and v.reason == "precheck_error"


@pytest.mark.asyncio
async def test_the_jobs_mandate_wins_and_the_job_user_is_the_identity():
    seen = []
    spec = dict(EXIT_SPEC, params=dict(EXIT_SPEC["params"], mandate="m_other"))
    await P.run_precheck(_job({"precheck": spec, "mandate": "m_own"}, user="owner1"),
                         data_dir="x", tool_factory=_mark_tool({"barrier": "none"}, seen))
    assert seen[0].mandate == "m_own" and seen[1] == "owner1"
    from core.exec_identity import current_exec_identity
    assert current_exec_identity()[0] == ""  # reset after the read


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
        raise AssertionError("a cancel_when tick must not start a session")


@pytest.mark.asyncio
async def test_runner_cancels_an_orphan_job_and_runs_no_model(tmp_path, monkeypatch, log):
    from core.runtime_paths import cron_db_path
    from cron.runner import make_agent_runner
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    svc = CronService(CronJobStore(cron_db_path(str(tmp_path))))
    job = svc.schedule(task="exit job for MEME", schedule_spec="15m", user_id="rob",
                       payload={"precheck": EXIT_SPEC, "mandate": "m1"},
                       )

    async def closed(j, *, data_dir, tool_factory=None):
        return P.PrecheckVerdict(False, P.ORPHAN_VIA, {"precheck": "defi_data.position_mark",
                                                       "flag": "closed"}, cancel="closed")
    monkeypatch.setattr(P, "run_precheck", closed)

    async def no_run(*a, **k):
        raise AssertionError("no model run on an orphan")
    monkeypatch.setattr("cron.runner._run_task_to_outcome", no_run)
    assert await make_agent_runner(_Agent(), data_dir=str(tmp_path))(job) is True
    after = svc.store.get(job.id, user_id="rob")
    assert after.status == "cancelled"
    assert (after.payload.get("stopped") or {}).get("via") == "orphan"
    rows = log.query(kind="cron_run")
    assert [r["attrs"]["reason"] for r in rows] == ["orphan"]
