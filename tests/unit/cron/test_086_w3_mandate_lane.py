"""086 W3: a trading mandate's jobs are in the lane ``mandate:<id>`` — a
``/job stop --forever`` on one exit job stops that mandate's lane only."""
import pytest

from core import cron_lanes
from core.cron_lanes import LaneStopped, gated_lanes, job_lane
from cron.jobs import CronJobStore
from cron.service import CronService


@pytest.fixture(autouse=True)
def _pins_resolve(monkeypatch):
    monkeypatch.setattr(cron_lanes, "missing_pinned_skills", lambda payload, user_id: [])


def _svc(tmp_path):
    return CronService(CronJobStore(str(tmp_path / "cron.db")))


def _exit_payload(mid):
    return {"mandate": mid, "skills": ["exits"], "rig": "money_rail"}


def test_the_mandate_lane_wins_and_is_gated_first():
    assert job_lane(_exit_payload("M1")) == "mandate:m1"
    assert gated_lanes(_exit_payload("m1")) == ["mandate:m1", "exits", "money_rail"]
    assert job_lane({"skills": ["exits"]}) == "exits"
    assert job_lane({"mandate": "  "}) == ""


def test_stop_forever_on_one_exit_job_stops_only_that_mandate(tmp_path):
    from surfaces.telegram.job_ops import jobs_reply
    svc = _svc(tmp_path)
    job = svc.schedule(task="exit MEME", schedule_spec="15m", user_id="u1",
                       payload=_exit_payload("m1"))
    out = jobs_reply("u1", str(tmp_path), ["stop", job.id[:8], "--forever"])
    assert "`mandate:m1` lane is stopped" in out
    with pytest.raises(LaneStopped):
        svc.schedule(task="exit MEME2", schedule_spec="15m", user_id="u1",
                     payload=_exit_payload("m1"))
    # Another mandate's exit job and a plain `exits` job are not stopped.
    assert svc.schedule(task="exit MEME3", schedule_spec="15m", user_id="u1",
                        payload=_exit_payload("m2")).status == "scheduled"
    assert svc.schedule(task="exit by hand", schedule_spec="15m", user_id="u1",
                        payload={"skills": ["exits"]}).status == "scheduled"
    assert "Re-armed `mandate:m1`" in jobs_reply("u1", str(tmp_path),
                                                 ["allow", "mandate:m1"])
    assert svc.schedule(task="exit MEME2", schedule_spec="15m", user_id="u1",
                        payload=_exit_payload("m1")).status == "scheduled"


# -- the agent may not edit a mandate's job -----------------------------------

def _agent_tool(tmp_path, monkeypatch, *, owner_turn):
    import types as _t
    import tools.goal_tools as gt
    from tools.cronjob_tools import CronJobTool
    monkeypatch.setattr(gt, "owner_seat_turn", lambda ctx: owner_turn)
    monkeypatch.setattr(CronJobTool, "_self_scheduling_refusal",
                        staticmethod(lambda ctx: None))
    t = object.__new__(CronJobTool)
    t._cron_service = _svc(tmp_path)
    ctx = _t.SimpleNamespace(user_id="u1", role="orchestrator", is_sub_agent=False,
                             metadata={}, session_id=None)
    return t, ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [dict(precheck={}), dict(old_text="exit", new_text="hold"),
                                    dict(schedule="1h"), dict(skills=[])])
async def test_an_agent_edit_of_a_mandate_job_is_refused(tmp_path, monkeypatch, change):
    from tools.cronjob_tools import CronEditAction
    t, ctx = _agent_tool(tmp_path, monkeypatch, owner_turn=False)
    job = t._cron_service.schedule(task="exit MEME", schedule_spec="15m", user_id="u1",
                                   payload=_exit_payload("m1"))
    res = await t.cronjob_edit(CronEditAction(job_id=job.id, **change), execution_context=ctx)
    assert res.error and "belongs to a trading mandate" in res.error
    after = t._cron_service.store.get(job.id)
    assert after.payload["mandate"] == "m1" and after.task == "exit MEME"


@pytest.mark.asyncio
async def test_an_owner_turn_may_edit_and_a_plain_job_stays_editable(tmp_path, monkeypatch):
    from tools.cronjob_tools import CronEditAction
    t, ctx = _agent_tool(tmp_path, monkeypatch, owner_turn=True)
    job = t._cron_service.schedule(task="exit MEME", schedule_spec="15m", user_id="u1",
                                   payload=_exit_payload("m1"))
    res = await t.cronjob_edit(CronEditAction(job_id=job.id, schedule="30m"),
                               execution_context=ctx)
    assert res.error is None, res.error
    assert t._cron_service.store.get(job.id).payload["mandate"] == "m1"
    t, ctx = _agent_tool(tmp_path, monkeypatch, owner_turn=False)
    plain = t._cron_service.schedule(task="watch a pool", schedule_spec="1h", user_id="u1")
    res = await t.cronjob_edit(CronEditAction(job_id=plain.id, schedule="2h"),
                               execution_context=ctx)
    assert res.error is None, res.error
