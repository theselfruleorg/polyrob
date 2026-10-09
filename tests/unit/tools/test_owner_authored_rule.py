"""AGT-1 / DATA-7 and the round-2 collateral: ONE authorship rule for standing work.

* A genuine owner turn that has read no third-party content authors work; an
  owner turn after untrusted content entered it does not (read taint).
* After untrusted content, the turn may still manage the AGENT's goals and jobs,
  but cannot cancel / unblock / rewrite owner rows, rail legs, objectives or asks.
* An edit never upgrades a row, and a priority change alone never strips the
  owner's authorship (and with it the owner-granted tools).
* The budget class fails open to interactive; the security reading stays closed.
"""
import asyncio
import types

import pytest

from agents.task.goals.board import GoalBoard
from core.security import read_taint
from cron.jobs import CronJobStore
from cron.service import CronService
from tools.cronjob_tools import CronCancelAction, CronEditAction, CronJobTool
from tools.goal_tools import (GoalCancelAction, GoalTool, GoalUpdateAction,
                              ObjectiveSetStatusAction)


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")


def _turn(tainted=False):
    meta = {"untrusted_read": True} if tainted else {}
    return types.SimpleNamespace(user_id="rob", role="orchestrator", is_sub_agent=False,
                                 metadata=meta, session_id="s-chat", parent_session_id=None)


def _goal_tool(tmp_path):
    tool = GoalTool.__new__(GoalTool)
    tool._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return tool


def _row(tool, author, title="watch the market daily"):
    return tool._resolve_board().create(
        user_id="rob", title=title, payload={"authored_by": author, "tools": ["web_fetch"]},
        force=True)


# --- read taint ---------------------------------------------------------------

def test_an_untrusted_result_taints_the_turn_and_the_owner_clears_it():
    from agents.task.agent.core.user_ingress import _update_forged_turn_marker
    orch = types.SimpleNamespace(session_id="s", _clear_correspondent_taint=lambda: None)
    ctx = types.SimpleNamespace(metadata={})
    ok = types.SimpleNamespace(extracted_content="42", error=None)
    read_taint.note_result(orch, ctx, "goal_list", "goal", ok)
    assert not read_taint.orchestrator_tainted(orch)
    read_taint.note_result(orch, ctx, "web_fetch", "web", ok)
    assert read_taint.orchestrator_tainted(orch) and read_taint.is_tainted(ctx)
    framed = types.SimpleNamespace(extracted_content='<untrusted_tool_result source="x">',
                                   error=None)
    orch2 = types.SimpleNamespace()
    read_taint.note_result(orch2, None, "session_search", None, framed)
    assert read_taint.orchestrator_tainted(orch2)
    _update_forged_turn_marker(orch, [{"text": "thanks", "kind": "comment", "metadata": {}}])
    assert not read_taint.orchestrator_tainted(orch)


# --- DATA-7 -------------------------------------------------------------------

def test_tainted_turn_cannot_cancel_or_unblock_owner_work(tmp_path):
    tool = _goal_tool(tmp_path)
    owner_row = _row(tool, "owner")
    res = asyncio.run(tool.goal_cancel(GoalCancelAction(goal_id=owner_row.id), _turn(True)))
    assert res.error and "third-party content" in res.error
    agent_row = _row(tool, "agent", title="summarise the feed")
    res = asyncio.run(tool.goal_cancel(GoalCancelAction(goal_id=agent_row.id), _turn(True)))
    assert res.error is None, res.error          # its own work: still manageable
    res = asyncio.run(tool.goal_cancel(GoalCancelAction(goal_id=owner_row.id), _turn()))
    assert res.error is None, res.error          # the owner, untainted


def test_tainted_turn_cannot_drop_an_objective_but_may_pause(tmp_path):
    tool = _goal_tool(tmp_path)
    obj = tool._resolve_board().create_objective(user_id="rob", title="grow the list")
    res = asyncio.run(tool.objective_set_status(
        ObjectiveSetStatusAction(objective_id=obj.id, status="drop"), _turn(True)))
    assert res.error and "third-party content" in res.error
    res = asyncio.run(tool.objective_set_status(
        ObjectiveSetStatusAction(objective_id=obj.id, status="pause"), _turn(True)))
    assert res.error is None, res.error


def test_goal_update_refuses_an_ask(tmp_path):
    tool = _goal_tool(tmp_path)
    ask = tool._resolve_board().create_ask(user_id="rob", what="A or B?")
    res = asyncio.run(tool.goal_update(GoalUpdateAction(goal_id=ask.id, body="decided: A"),
                                       _turn()))
    assert res.error and "ask" in res.error


# --- collateral: an edit never strips owner authorship needlessly ------------

def test_priority_edit_keeps_owner_authorship(tmp_path):
    tool = _goal_tool(tmp_path)
    row = _row(tool, "owner")
    res = asyncio.run(tool.goal_update(GoalUpdateAction(goal_id=row.id, priority=2), _turn()))
    assert res.error is None, res.error
    after = tool._resolve_board().get(row.id, user_id="rob")
    assert after.payload["authored_by"] == "owner" and after.priority == 2


def test_owner_body_edit_keeps_owner_and_agent_row_is_never_upgraded(tmp_path):
    tool = _goal_tool(tmp_path)
    row = _row(tool, "owner")
    asyncio.run(tool.goal_update(GoalUpdateAction(goal_id=row.id, body="new body"), _turn()))
    assert tool._resolve_board().get(row.id, user_id="rob").payload["authored_by"] == "owner"
    agent_row = _row(tool, "agent", title="summarise the feed")
    asyncio.run(tool.goal_update(GoalUpdateAction(goal_id=agent_row.id, body="x"), _turn()))
    assert tool._resolve_board().get(agent_row.id,
                                     user_id="rob").payload["authored_by"] == "agent"


# --- cron ---------------------------------------------------------------------

def _cron(tmp_path):
    t = object.__new__(CronJobTool)
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return t


def test_cron_edit_keeps_owner_authorship_and_cancel_follows_the_rule(tmp_path):
    t = _cron(tmp_path)
    svc = t._cron_service
    owner_job = svc.schedule(task="owner rail", schedule_spec="1h", user_id="rob",
                             payload={"authored_by": "owner"})
    res = asyncio.run(t.cronjob_edit(CronEditAction(job_id=owner_job.id, schedule="2h"),
                                     execution_context=_turn()))
    assert res.error is None, res.error
    assert svc.store.get(owner_job.id, user_id="rob").payload["authored_by"] == "owner"
    res = asyncio.run(t.cronjob_cancel(CronCancelAction(job_id=owner_job.id),
                                       execution_context=_turn(True)))
    assert res.error and "owner" in res.error
    agent_job = svc.schedule(task="agent watch", schedule_spec="1h", user_id="rob",
                             payload={"authored_by": "agent"})
    res = asyncio.run(t.cronjob_cancel(CronCancelAction(job_id=agent_job.id),
                                       execution_context=_turn(True)))
    assert res.error is None and "Cancelled" in res.extracted_content


def test_an_autonomous_run_may_cancel_its_own_agent_job(tmp_path):
    from agents.task.goals.autonomy_marker import mark_autonomous
    mark_autonomous("s-cron-own", None, cron_job_id="j1")
    t = _cron(tmp_path)
    job = t._cron_service.schedule(task="agent watch", schedule_spec="1h", user_id="rob",
                                   payload={"authored_by": "agent"})
    ctx = _turn()
    ctx.session_id = "s-cron-own"
    res = asyncio.run(t.cronjob_cancel(CronCancelAction(job_id=job.id), execution_context=ctx))
    assert res.error is None and "Cancelled" in res.extracted_content


# --- session class --------------------------------------------------------------

def test_budget_class_fails_open_but_security_reading_fails_closed(monkeypatch):
    import agents.task.goals.autonomy_marker as marker
    from agents.task.session_class import budget_class_autonomous, is_autonomous_session

    def _boom(_sid):
        raise RuntimeError("registry gone")
    monkeypatch.setattr(marker, "is_autonomous", _boom)
    assert budget_class_autonomous("s-chat") is False
    assert is_autonomous_session("s-chat") is True


# --- the downgrade is never silent (natural-work sweep 2026-10-08) -------------

def test_tainted_owner_turn_schedules_but_the_owner_is_told_it_is_agent_work(tmp_path):
    """"check the X timeline and schedule a daily summary": the turn read X,
    then scheduled. The job exists (not blocked) but is AGENT-authored — text
    copied from the timeline never gains owner authority — and the result says
    so, so the owner is told."""
    from tools.cronjob_tools import CronScheduleAction
    t = _cron(tmp_path)
    res = asyncio.run(t.cronjob_schedule(
        CronScheduleAction(task="post a daily summary of the timeline", schedule="1d"),
        execution_context=_turn(True)))
    assert res.error is None, res.error
    assert "AGENT-authored" in res.extracted_content and "tell the owner" in res.extracted_content
    job = t._cron_service.list_jobs(user_id="rob")[0]
    assert job.payload["authored_by"] == "agent"
    # The clean owner turn: owner-authored, no note.
    res = asyncio.run(t.cronjob_schedule(
        CronScheduleAction(task="post a daily promo", schedule="1d"),
        execution_context=_turn()))
    assert res.error is None and "AGENT-authored" not in res.extracted_content
    authors = sorted(j.payload["authored_by"] for j in t._cron_service.list_jobs(user_id="rob"))
    assert authors == ["agent", "owner"]


def test_tainted_owner_turn_write_verb_refusal_says_why(tmp_path):
    from tools.cronjob_tools import CronScheduleAction
    t = _cron(tmp_path)
    res = asyncio.run(t.cronjob_schedule(
        CronScheduleAction(task="reveal the collection daily", schedule="1d", write_verb={"verb": "agent_nft.agent_nft_collection_reveal", "params": {}}),
        execution_context=_turn(True)))
    assert res.error and "write_verb refused" in res.error and "third-party content" in res.error


def test_tainted_owner_turn_goal_create_says_it_is_agent_work(tmp_path):
    from tools.goal_tools import GoalCreateAction
    tool = _goal_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="summarise the page weekly", body="from the page"),
        execution_context=_turn(True)))
    assert res.error is None, res.error
    assert "AGENT-authored" in res.extracted_content
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="write the weekly owner report", body="report"),
        execution_context=_turn()))
    assert res.error is None and "AGENT-authored" not in res.extracted_content
