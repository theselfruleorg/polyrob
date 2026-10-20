"""Work order 0011 — an owner stop is state, not prose.

The owner stops a cron LANE (the job's first pinned skill, else its rig) with
`/job stop <id> --forever`; ``CronService.schedule`` then refuses a create that
is not the owner's in that lane and names the stop. The owner's own create
re-arms the lane; `/job allow <lane>` removes the stop. A job created while
`/pause cron` is on is stored paused and runs after the owner resumes.
"""
import types
from datetime import datetime, timedelta

import pytest

from core import cron_lanes
from core.cron_lanes import LaneStopped, job_lane
from core.goal_suppressions import (SCOPE_CRON_LANE, SCOPE_TITLE, active, lane_stop,
                                    stops_db_beside, suppress)
from cron.jobs import CronJobStore
from cron.service import CronService


@pytest.fixture(autouse=True)
def _lane_ids_are_skills(monkeypatch):
    """The lanes here (`buyback`, …) are lane names, not shipped skills: the pin
    validator (081 WS-5.4, tested in test_pinned_skill_validation) passes them."""
    monkeypatch.setattr(cron_lanes, "missing_pinned_skills", lambda payload, user_id: [])


def _svc(tmp_path):
    return CronService(CronJobStore(str(tmp_path / "cron.db")))


def _stop_lane(tmp_path, lane="buyback", user="u1"):
    return suppress(str(tmp_path / "goals.db"), user_id=user, scope=SCOPE_CRON_LANE,
                    value=lane, label=lane, reason="no more buybacks")


# --- the lane rule ------------------------------------------------------------

def test_lane_is_first_skill_else_rig_else_none():
    assert job_lane({"skills": ["Buyback", "x"], "rig": "money_rail"}) == "buyback"
    assert job_lane({"rig": "Money_Rail"}) == "money_rail"
    assert job_lane({"skills": [], "rig": ""}) == ""
    assert job_lane(None) == ""


def test_stop_db_is_goals_db_beside_cron_db(tmp_path):
    assert stops_db_beside(str(tmp_path / "cron.db")) == str(tmp_path / "goals.db")


# --- schedule() enforces the stop ---------------------------------------------

def test_agent_create_in_stopped_lane_is_refused_naming_the_time(tmp_path):
    _stop_lane(tmp_path)
    with pytest.raises(LaneStopped) as e:
        _svc(tmp_path).schedule(task="buy back", schedule_spec="1h", user_id="u1",
                                payload={"skills": ["buyback"]})
    msg = str(e.value)
    assert "the owner stopped `buyback` at" in msg and "UTC" in msg
    assert "/job allow buyback" in msg
    assert _svc(tmp_path).list_jobs(user_id="u1") == []


def test_owner_seat_create_succeeds_and_lifts_the_stop(tmp_path):
    _stop_lane(tmp_path)
    job = _svc(tmp_path).schedule(task="buy back", schedule_spec="1h", user_id="u1",
                                  payload={"skills": ["buyback"]}, owner_seat=True)
    assert job.status == "scheduled"
    assert any("is lifted" in n for n in cron_lanes.schedule_notes(job))
    assert lane_stop(str(tmp_path / "goals.db"), user_id="u1", lane="buyback") is None


def test_payload_authorship_is_not_owner_provenance(tmp_path):
    _stop_lane(tmp_path)
    with pytest.raises(LaneStopped):
        _svc(tmp_path).schedule(task="buy back", schedule_spec="1h", user_id="u1",
                                payload={"skills": ["buyback"], "authored_by": "owner"})


def test_other_lane_and_no_lane_and_other_tenant_unaffected(tmp_path):
    _stop_lane(tmp_path)
    svc = _svc(tmp_path)
    assert svc.schedule(task="post", schedule_spec="1h", user_id="u1",
                        payload={"skills": ["engagement"]}).status == "scheduled"
    assert svc.schedule(task="plain", schedule_spec="1h", user_id="u1").status == "scheduled"
    assert svc.schedule(task="buy", schedule_spec="1h", user_id="u2",
                        payload={"skills": ["buyback"]}).status == "scheduled"


def test_a_stopped_skill_pinned_second_is_refused(tmp_path):
    # Review of 0011: the gate reads every pinned skill and the rig, not only
    # the lane (skills[0]) — reordering the pins does not get past a stop.
    _stop_lane(tmp_path)
    with pytest.raises(LaneStopped) as e:
        _svc(tmp_path).schedule(task="notes then buy", schedule_spec="1h", user_id="u1",
                                payload={"skills": ["notes", "buyback"]})
    assert "`buyback`" in str(e.value)
    _stop_lane(tmp_path, lane="money_rail")
    with pytest.raises(LaneStopped):
        _svc(tmp_path).schedule(task="buy", schedule_spec="1h", user_id="u1",
                                payload={"skills": ["notes"], "rig": "money_rail"})


def test_owner_create_with_an_unreadable_stop_store_says_so(tmp_path, monkeypatch):
    import core.goal_suppressions as gs

    def broken(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(gs, "lane_stop", broken)
    job = _svc(tmp_path).schedule(task="buy back", schedule_spec="1h", user_id="u1",
                                  payload={"skills": ["buyback"]}, owner_seat=True)
    assert job.status == "scheduled"
    assert any("could not read your stops" in n for n in cron_lanes.schedule_notes(job))


def test_rig_lane_is_stopped_when_no_skill(tmp_path):
    _stop_lane(tmp_path, lane="money_rail")
    with pytest.raises(LaneStopped):
        _svc(tmp_path).schedule(task="buy", schedule_spec="1h", user_id="u1",
                                payload={"rig": "money_rail"})


# --- /pause cron: created paused ----------------------------------------------

def _paused(monkeypatch, scopes=("cron",)):
    import core.autonomy_control as ac
    state = {"on": True}

    def allows(kind, data_dir=None):
        denied = state["on"] and kind in ("cron_run",) + (("digest",) if "all" in scopes else ())
        return types.SimpleNamespace(allowed=not denied,
                                     state=types.SimpleNamespace(scopes=tuple(scopes)))
    monkeypatch.setattr(ac, "allows", allows)
    return state


def test_created_paused_under_pause_cron_then_released_on_resume(tmp_path, monkeypatch):
    state = _paused(monkeypatch)
    svc = _svc(tmp_path)
    job = svc.schedule(task="digest the week", schedule_spec="1h", user_id="u1")
    row = svc.store.get(job.id)
    assert row.status == "paused" and row.next_run_at is None
    assert row.payload["paused"]["by"] == cron_lanes.PAUSE_SCOPE_BY
    assert any("/resume cron" in n for n in cron_lanes.schedule_notes(job))
    from cron.scheduler import release_scope_paused
    now = datetime.now()
    assert release_scope_paused(svc.store, now) == []  # still paused
    state["on"] = False
    assert release_scope_paused(svc.store, now) == [job.id]
    row = svc.store.get(job.id)
    assert row.status == "scheduled" and row.next_run_at is not None
    assert "paused" not in row.payload


def test_owner_job_resume_runs_a_scope_paused_job(tmp_path, monkeypatch):
    _paused(monkeypatch)
    svc = _svc(tmp_path)
    job = svc.schedule(task="digest the week", schedule_spec="1h", user_id="u1")
    assert svc.resume(job.id, user_id="u1", via="telegram")
    assert svc.store.get(job.id).status == "scheduled"


def test_owner_paused_job_is_not_released_by_the_scope(tmp_path):
    from cron.scheduler import release_scope_paused
    svc = _svc(tmp_path)
    job = svc.schedule(task="digest", schedule_spec="1h", user_id="u1")
    svc.pause(job.id, user_id="u1", via="telegram")
    assert release_scope_paused(svc.store, datetime.now()) == []
    assert svc.store.get(job.id).status == "paused"


# --- the owner verbs: /job stop --forever, /job allow, /jobs ------------------

def test_job_stop_forever_then_allow_round_trip(tmp_path):
    from surfaces.telegram.job_ops import jobs_reply
    svc = _svc(tmp_path)
    job = svc.schedule(task="buy back", schedule_spec="1h", user_id="u1",
                       payload={"skills": ["buyback"]})
    out = jobs_reply("u1", str(tmp_path), ["stop", job.id[:8], "--forever"])
    assert "Stopped job" in out and "`buyback` lane is stopped" in out
    assert svc.store.get(job.id).status == "cancelled"
    listing = jobs_reply("u1", str(tmp_path), ["list"])
    assert "Stopped by you (1)" in listing and "/job allow buyback" in listing
    with pytest.raises(LaneStopped):
        svc.schedule(task="buy back", schedule_spec="1h", user_id="u1",
                     payload={"skills": ["buyback"]})
    assert "Re-armed `buyback`" in jobs_reply("u1", str(tmp_path), ["allow", "buyback"])
    assert "Stopped by you" not in jobs_reply("u1", str(tmp_path), ["list"])
    assert svc.schedule(task="buy back", schedule_spec="1h", user_id="u1",
                        payload={"skills": ["buyback"]}).status == "scheduled"


def test_job_show_names_the_lane_and_its_stop(tmp_path):
    from surfaces.telegram.job_ops import jobs_reply
    svc = _svc(tmp_path)
    job = svc.schedule(task="buy back", schedule_spec="1h", user_id="u1",
                       payload={"skills": ["buyback"]})
    shown = jobs_reply("u1", str(tmp_path), ["show", job.id[:8]])
    assert "lane `buyback`" in shown and "stopped by you" not in shown
    jobs_reply("u1", str(tmp_path), ["stop", job.id[:8], "--forever"])
    shown = jobs_reply("u1", str(tmp_path), ["show", job.id[:8]])
    assert "lane `buyback` — stopped by you since" in shown
    assert "/job allow buyback" in shown
    bare = svc.schedule(task="plain", schedule_spec="1h", user_id="u1")
    assert "lane" not in jobs_reply("u1", str(tmp_path), ["show", bare.id[:8]])


def test_job_stop_once_keeps_the_lane_open(tmp_path):
    from surfaces.telegram.job_ops import jobs_reply
    svc = _svc(tmp_path)
    job = svc.schedule(task="buy back", schedule_spec="1h", user_id="u1",
                       payload={"skills": ["buyback"]})
    out = jobs_reply("u1", str(tmp_path), ["stop", job.id[:8]])
    assert "--forever" in out
    assert lane_stop(str(tmp_path / "goals.db"), user_id="u1", lane="buyback") is None


def test_cli_stop_forever_and_allow(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from cli.commands import cron as cron_cli
    svc = _svc(tmp_path)
    monkeypatch.setattr(cron_cli, "_service", lambda write=None: svc)
    job = svc.schedule(task="buy back", schedule_spec="1h", user_id="u1",
                       payload={"rig": "money_rail"})
    res = CliRunner().invoke(cron_cli.job, ["stop", job.id[:8], "--forever", "--user", "u1"])
    assert res.exit_code == 0 and "lane is stopped" in res.output
    res = CliRunner().invoke(cron_cli.job, ["list", "--user", "u1"])
    assert "polyrob job allow money_rail" in res.output
    res = CliRunner().invoke(cron_cli.job, ["allow", "money_rail", "--user", "u1"])
    assert res.exit_code == 0 and "Re-armed" in res.output


def test_prune_never_touches_the_stop(tmp_path):
    svc = _svc(tmp_path)
    job = svc.schedule(task="buy back", schedule_spec="1h", user_id="u1",
                       payload={"skills": ["buyback"]})
    cron_lanes.stop_job(svc, job, user_id="u1", via="cli", forever=True)
    assert svc.store.prune_cancelled(older_than_days=0,
                                     now=datetime.now() + timedelta(days=30)) == 1
    assert lane_stop(str(tmp_path / "goals.db"), user_id="u1", lane="buyback") is not None


def test_goal_views_do_not_list_lane_stops(tmp_path):
    from agents.task.goals.board import GoalBoard
    board = GoalBoard(str(tmp_path / "goals.db"))
    _stop_lane(tmp_path)
    suppress(board.db_path, user_id="u1", scope=SCOPE_TITLE, value="Post daily")
    assert [s.scope for s in board.suppressions(user_id="u1")] == [SCOPE_TITLE]
    assert {s.scope for s in active(board.db_path, user_id="u1")} == {SCOPE_TITLE,
                                                                     SCOPE_CRON_LANE}


# --- the agent tool -----------------------------------------------------------

def _tool(tmp_path, monkeypatch, *, owner_turn: bool):
    import tools.goal_tools as gt
    from tools.cronjob_tools import CronJobTool
    monkeypatch.setattr(gt, "owner_seat_turn", lambda ctx: owner_turn)
    monkeypatch.setattr(CronJobTool, "_self_scheduling_refusal",
                        staticmethod(lambda ctx: None))
    t = object.__new__(CronJobTool)
    t._cron_service = _svc(tmp_path)
    return t


def _ctx():
    return types.SimpleNamespace(user_id="u1", role="orchestrator", is_sub_agent=False,
                                 metadata={}, session_id=None)


@pytest.mark.asyncio
async def test_agent_tool_schedule_refused_in_stopped_lane(tmp_path, monkeypatch):
    from tools.cronjob_tools import CronScheduleAction
    _stop_lane(tmp_path)
    t = _tool(tmp_path, monkeypatch, owner_turn=False)
    res = await t.cronjob_schedule(CronScheduleAction(
        task="buy back the token", schedule="1h", skills=["buyback"]), execution_context=_ctx())
    assert res.error and "cronjob_schedule refused" in res.error
    assert "the owner stopped `buyback` at" in res.error


@pytest.mark.asyncio
async def test_agent_tool_never_lifts_the_stop_even_on_an_owner_turn(tmp_path, monkeypatch):
    # Review of 0011: the model's own call on an owner turn is not the owner's
    # re-arm (it may come from recall). Only a typed seat lifts a stop.
    from tools.cronjob_tools import CronScheduleAction
    _stop_lane(tmp_path)
    t = _tool(tmp_path, monkeypatch, owner_turn=True)
    res = await t.cronjob_schedule(CronScheduleAction(
        task="buy back the token", schedule="1h", skills=["buyback"]), execution_context=_ctx())
    assert res.error and "/job allow buyback" in res.error
    assert lane_stop(str(tmp_path / "goals.db"), user_id="u1", lane="buyback") is not None
    assert t._cron_service.list_jobs(user_id="u1") == []


@pytest.mark.asyncio
async def test_agent_edit_on_owner_turn_may_not_add_a_stopped_skill(tmp_path, monkeypatch):
    from tools.cronjob_tools import CronEditAction
    _stop_lane(tmp_path)
    t = _tool(tmp_path, monkeypatch, owner_turn=True)
    job = t._cron_service.schedule(task="x", schedule_spec="1h", user_id="u1",
                                   payload={"skills": ["engagement"]})
    res = await t.cronjob_edit(CronEditAction(job_id=job.id, skills=["engagement", "buyback"]),
                               execution_context=_ctx())
    assert res.error and "cronjob_edit refused" in res.error


@pytest.mark.asyncio
async def test_agent_cancel_on_owner_turn_records_owner_turn(tmp_path, monkeypatch):
    from tools.cronjob_tools import CronCancelAction
    t = _tool(tmp_path, monkeypatch, owner_turn=True)
    job = t._cron_service.schedule(task="x", schedule_spec="1h", user_id="u1")
    await t.cronjob_cancel(CronCancelAction(job_id=job.id), execution_context=_ctx())
    assert t._cron_service.store.get(job.id).payload["stopped"]["via"] == "owner_turn"


@pytest.mark.asyncio
async def test_agent_cancel_forever_only_on_owner_turn(tmp_path, monkeypatch):
    from tools.cronjob_tools import CronCancelAction
    t = _tool(tmp_path, monkeypatch, owner_turn=False)
    job = t._cron_service.schedule(task="x", schedule_spec="1h", user_id="u1",
                                   payload={"skills": ["buyback"]})
    res = await t.cronjob_cancel(CronCancelAction(job_id=job.id, forever=True),
                                 execution_context=_ctx())
    assert res.error and "only the owner" in res.error
    assert t._cron_service.store.get(job.id).status == "scheduled"
    t = _tool(tmp_path, monkeypatch, owner_turn=True)
    res = await t.cronjob_cancel(CronCancelAction(job_id=job.id, forever=True),
                                 execution_context=_ctx())
    assert "lane is stopped" in res.extracted_content
    assert t._cron_service.store.get(job.id).payload["stopped"]["via"] == "owner_turn"
    assert lane_stop(str(tmp_path / "goals.db"), user_id="u1", lane="buyback") is not None


@pytest.mark.asyncio
async def test_agent_edit_may_not_move_a_job_into_a_stopped_lane(tmp_path, monkeypatch):
    from tools.cronjob_tools import CronEditAction
    _stop_lane(tmp_path)
    t = _tool(tmp_path, monkeypatch, owner_turn=False)
    job = t._cron_service.schedule(task="x", schedule_spec="1h", user_id="u1",
                                   payload={"skills": ["engagement"]})
    res = await t.cronjob_edit(CronEditAction(job_id=job.id, skills=["buyback"]),
                               execution_context=_ctx())
    assert res.error and "cronjob_edit refused" in res.error
