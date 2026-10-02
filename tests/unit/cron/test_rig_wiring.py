"""057 WS-A: rigs reach the cron rail — tool, store edit, and run request."""
import types

import pytest

from cron.jobs import CronJobStore
from cron.rig_edit import set_job_rig
from cron.runner import resolve_cron_tools
from cron.service import CronService
from core.config_policy.rigs import RIGS
from tools.cronjob_tools import CronJobTool, CronScheduleAction


def _tool(tmp_path):
    t = object.__new__(CronJobTool)
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return t


def _ctx(user="u1"):
    """A genuine owner turn (M07: cronjob_schedule refuses any other)."""
    return types.SimpleNamespace(user_id=user, role="orchestrator",
                                 is_sub_agent=False, metadata={}, session_id=None)


def test_resolve_cron_tools_is_unchanged_by_default(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_RIG_DEFAULT", raising=False)
    from cron.runner import default_cron_tools
    assert resolve_cron_tools({}) == default_cron_tools()
    assert resolve_cron_tools({"tools": ["filesystem"]}) == ["filesystem"]


def test_resolve_cron_tools_honours_the_job_rig(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_RIG_DEFAULT", raising=False)
    assert resolve_cron_tools({"rig": "ops"}) == list(RIGS["ops"])
    # payload.tools still wins — the owner-grant contract is untouched.
    assert resolve_cron_tools({"rig": "ops", "tools": ["defi_trade"]}) == ["defi_trade"]


@pytest.mark.asyncio
async def test_cronjob_schedule_stores_a_valid_rig(tmp_path):
    t = _tool(tmp_path)
    res = await t.cronjob_schedule(
        CronScheduleAction(task="read the board and report", schedule="*/15 * * * *",
                           rig="research"),
        execution_context=_ctx())
    assert res.error is None
    job = t._cron_service.list_jobs(user_id="u1")[0]
    assert job.payload["rig"] == "research"
    assert job.payload["authored_by"] == "agent"  # H05 provenance


@pytest.mark.asyncio
@pytest.mark.parametrize("rig,leaks", [("money_rail", "defi_trade"), ("ops", "cronjob")])
async def test_cronjob_schedule_refuses_a_rig_past_the_self_goal_ceiling(
        tmp_path, monkeypatch, rig, leaks):
    """H05 (2026-09-23): `rig=` used to be stored unchecked, so an agent-set
    `money_rail` resolved to `defi_trade` at dispatch. Previously this test
    stored `rig="ops"` from the agent tool — that encoded the widening."""
    import tools.goal_tools as gt
    monkeypatch.setattr(gt, "allowed_self_goal_tools", lambda: gt._SELF_GOAL_ALLOWED_TOOLS)
    t = _tool(tmp_path)
    res = await t.cronjob_schedule(
        CronScheduleAction(task="read the board and report", schedule="*/15 * * * *",
                           rig=rig),
        execution_context=_ctx())
    assert res.error and "NOT granted" in res.error and leaks in res.error
    assert t._cron_service.list_jobs(user_id="u1") == []


def test_resolve_cron_tools_intersects_an_agent_rig_only(monkeypatch):
    import tools.goal_tools as gt
    monkeypatch.setattr(gt, "allowed_self_goal_tools", lambda: gt._SELF_GOAL_ALLOWED_TOOLS)
    agent = resolve_cron_tools({"rig": "money_rail", "authored_by": "agent"})
    assert "defi_trade" not in agent and "defi_data" in agent
    assert resolve_cron_tools({"rig": "money_rail"}) == list(RIGS["money_rail"])


@pytest.mark.asyncio
async def test_cronjob_schedule_refuses_an_unknown_rig_with_the_valid_list(tmp_path):
    t = _tool(tmp_path)
    res = await t.cronjob_schedule(
        CronScheduleAction(task="read the board and report", schedule="*/15 * * * *",
                           rig="opz"),
        execution_context=_ctx())
    assert res.error and "opz" in res.error and "ops" in res.error
    assert t._cron_service.list_jobs(user_id="u1") == []


def test_set_job_rig_merges_and_is_tenant_scoped(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    svc = CronService(store)
    job = svc.schedule(task="hourly buyback", schedule_spec="0 * * * *",
                       user_id="u1", payload={"deliver": "telegram"})
    assert set_job_rig(store, job.id, "money_rail", user_id="u1") is True
    got = store.get(job.id)
    assert got.payload["rig"] == "money_rail"
    assert got.payload["deliver"] == "telegram"  # merged, never replaced
    # unknown rig and wrong tenant both refuse
    assert set_job_rig(store, job.id, "nope", user_id="u1") is False
    assert set_job_rig(store, job.id, "ops", user_id="u2") is False
    # clearing works
    assert set_job_rig(store, job.id, None, user_id="u1") is True
    assert "rig" not in store.get(job.id).payload


def test_set_job_preflight_slot_cap_merges_and_clears(tmp_path):
    """2026-09-22: `payload.preflight = {"kind": "slot_cap", "max_open_rows": N}`
    is the SCOUT rail's $0 skip (cron/preflight.py); the setter merges like
    rig/deliver, refuses a non-positive cap, and `None` drops the key."""
    from cron.rig_edit import set_job_preflight_slot_cap
    store = CronJobStore(str(tmp_path / "cron.db"))
    svc = CronService(store)
    job = svc.schedule(task="scout", schedule_spec="0 * * * *", user_id="u1",
                       payload={"rig": "money_rail"})
    assert set_job_preflight_slot_cap(store, job.id, 6, user_id="u1") is True
    got = store.get(job.id)
    assert got.payload["preflight"] == {"kind": "slot_cap", "max_open_rows": 6}
    assert got.payload["rig"] == "money_rail"
    assert set_job_preflight_slot_cap(store, job.id, 0, user_id="u1") is False
    assert set_job_preflight_slot_cap(store, job.id, 6, user_id="u2") is False
    assert set_job_preflight_slot_cap(store, job.id, None, user_id="u1") is True
    assert "preflight" not in store.get(job.id).payload
