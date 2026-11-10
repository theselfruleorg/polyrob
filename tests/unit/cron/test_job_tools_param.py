"""107 WS-6: a job asks for the tools it needs; off an owner turn it holds the safe set."""
import types

import pytest

from cron.jobs import CronJobStore
from cron.service import CronService
from tools.cronjob_tools import (CronEditAction, CronJobTool, CronScheduleAction,
                                 resolve_cron_tools)
from tools.goal_tools import _SELF_GOAL_BASELINE_TOOLS


def _tool(tmp_path):
    t = object.__new__(CronJobTool)
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return t


def _owner_ctx():
    return types.SimpleNamespace(user_id="u1", role="orchestrator", is_sub_agent=False,
                                 metadata={}, session_id=None)


@pytest.fixture(autouse=True)
def bound_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")


@pytest.fixture
def autonomous_turn(monkeypatch):
    """A turn that is not an owner seat, on an instance that lets it schedule."""
    import tools.goal_tools as gt
    monkeypatch.setattr(gt, "owner_seat_turn", lambda ctx: False)
    monkeypatch.setattr(CronJobTool, "_self_scheduling_refusal", lambda self, ctx: None)


async def _schedule(t, **kw):
    return await t.cronjob_schedule(
        CronScheduleAction(task="scan the feed for news", schedule="every day 09:00", **kw),
        execution_context=_owner_ctx())


def _only_job(t):
    return t._cron_service.list_jobs(user_id="u1")[0]


@pytest.mark.asyncio
async def test_exact_tools_land_on_the_job_with_the_baseline(tmp_path):
    t = _tool(tmp_path)
    res = await _schedule(t, tools=["email"])
    assert res.error is None, res.error
    stored = _only_job(t).payload["tools"]
    assert "email" in stored and set(_SELF_GOAL_BASELINE_TOOLS) <= set(stored)
    assert "email" in resolve_cron_tools(_only_job(t).payload)


@pytest.mark.asyncio
async def test_a_money_tool_is_refused_and_names_the_way(tmp_path):
    t = _tool(tmp_path)
    res = await _schedule(t, tools=["defi_trade"])
    assert "money_rail" in res.error and "/job new" in res.error
    assert t._cron_service.list_jobs(user_id="u1") == []


@pytest.mark.asyncio
async def test_an_unknown_tool_is_refused(tmp_path):
    res = await _schedule(_tool(tmp_path), tools=["no_such_tool"])
    assert "no_such_tool" in res.error


@pytest.mark.asyncio
async def test_an_owner_turn_job_with_no_set_keeps_the_default_and_names_it(tmp_path):
    t = _tool(tmp_path)
    res = await _schedule(t)
    assert "tools" not in _only_job(t).payload
    assert "its runs carry:" in res.extracted_content


def _with_wide_default(monkeypatch):
    import tools.cronjob_tools as ct
    monkeypatch.setattr(ct, "default_cron_tools", lambda: ["filesystem", "task", "shell"])


@pytest.mark.asyncio
async def test_a_job_off_an_owner_turn_holds_the_default_inside_the_safe_set(
        tmp_path, autonomous_turn, monkeypatch):
    _with_wide_default(monkeypatch)
    t = _tool(tmp_path)
    res = await _schedule(t)
    assert res.error is None, res.error
    job = _only_job(t)
    assert "tools" not in job.payload and job.payload["actor"] == "agent"
    held = resolve_cron_tools(job.payload)
    assert "shell" not in held and "filesystem" in held
    # A stored list would widen the job past the narrow default and make it money-class.
    assert not set(held) - {"filesystem", "task"}


@pytest.mark.asyncio
async def test_full_off_an_owner_turn_is_held_the_same_way(tmp_path, autonomous_turn, monkeypatch):
    _with_wide_default(monkeypatch)
    t = _tool(tmp_path)
    await _schedule(t, rig="full")
    assert "shell" not in resolve_cron_tools(_only_job(t).payload)


@pytest.mark.asyncio
async def test_an_owner_turn_job_keeps_the_wide_default(tmp_path, monkeypatch):
    _with_wide_default(monkeypatch)
    t = _tool(tmp_path)
    await _schedule(t)
    assert "shell" in resolve_cron_tools(_only_job(t).payload)


@pytest.mark.asyncio
async def test_edit_sets_and_clears_the_tools(tmp_path):
    t = _tool(tmp_path)
    await _schedule(t)
    jid = _only_job(t).id
    res = await t.cronjob_edit(CronEditAction(job_id=jid, tools=["email"]),
                               execution_context=_owner_ctx())
    assert res.error is None, res.error
    assert "email" in _only_job(t).payload["tools"]
    assert _only_job(t).payload["actor"] == "owner_seat"
    await t.cronjob_edit(CronEditAction(job_id=jid, tools=[]), execution_context=_owner_ctx())
    assert not _only_job(t).payload.get("tools")


@pytest.mark.asyncio
async def test_an_autonomous_clear_of_the_tools_drops_an_owner_grant(tmp_path, monkeypatch):
    from tools.cronjob_tools import resolve_cron_tools as resolve
    t = _tool(tmp_path)
    await _schedule(t, tools=["email"])
    job = _only_job(t)
    t._cron_service.edit(job.id, user_id="u1", payload_updates={"rig": "money_rail"})
    import tools.goal_tools as gt
    monkeypatch.setattr(gt, "owner_seat_turn", lambda ctx: False)
    monkeypatch.setattr(CronJobTool, "_self_scheduling_refusal", lambda self, ctx: None)
    await t.cronjob_edit(CronEditAction(job_id=job.id, tools=[]), execution_context=_owner_ctx())
    job = _only_job(t)
    assert job.payload["actor"] == "agent"
    assert "defi_trade" not in resolve(job.payload)


@pytest.mark.asyncio
async def test_edit_refuses_a_money_tool(tmp_path):
    t = _tool(tmp_path)
    await _schedule(t)
    res = await t.cronjob_edit(CronEditAction(job_id=_only_job(t).id, tools=["defi_trade"]),
                               execution_context=_owner_ctx())
    assert "money_rail" in res.error
