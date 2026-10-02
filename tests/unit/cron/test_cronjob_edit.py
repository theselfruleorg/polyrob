"""cronjob_show / cronjob_edit — amend a live job in place (2026-09-24).

Why: the agent had only schedule/list/cancel, and list truncated the task to 60
chars. Resizing the buyback rail to 0.05 forced a cancel + full re-type of the
task, and the rewrite dropped the reconcile gate and the row-write ordering.
"""
import types

import pytest

import tools.goal_tools as gt
from cron.jobs import CronJobStore
from cron.service import CronService
from tools.cronjob_tools import CronEditAction, CronJobTool, CronShowAction

RULES = ("Buy 0.1 ETH of the token.\n"
         "SAFETY: reconcile the ledger before any swap.\n"
         "SAFETY: write the ledger row before the report.\n")


def _tool(tmp_path):
    t = object.__new__(CronJobTool)
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return t


def _ctx(user="u1"):
    return types.SimpleNamespace(user_id=user, role="orchestrator",
                                 is_sub_agent=False, metadata={}, session_id=None)


def _job(t, authored_by="owner", user="u1"):
    return t._cron_service.schedule(task=RULES, schedule_spec="1h", user_id=user,
                                    payload={"authored_by": authored_by, "deliver": "telegram"})


@pytest.fixture
def owner_turn(monkeypatch):
    monkeypatch.setattr(gt, "owner_seat_turn", lambda ctx: True)


@pytest.fixture
def agent_turn(monkeypatch):
    monkeypatch.setattr(gt, "owner_seat_turn", lambda ctx: False)


def test_actions_carry_decorator_metadata():
    for name in ("cronjob_show", "cronjob_edit"):
        fn = getattr(CronJobTool, name)
        assert hasattr(fn, "_description") and hasattr(fn, "_param_model")


@pytest.mark.asyncio
async def test_show_returns_the_full_task_and_provenance(tmp_path):
    t = _tool(tmp_path)
    job = _job(t)
    res = await t.cronjob_show(CronShowAction(job_id=job.id), execution_context=_ctx())
    assert res.error is None
    assert RULES.strip() in res.extracted_content
    assert "authored by: owner" in res.extracted_content
    assert "deliver: telegram" in res.extracted_content


@pytest.mark.asyncio
async def test_show_is_tenant_scoped(tmp_path):
    t = _tool(tmp_path)
    job = _job(t, user="tenant-a")
    res = await t.cronjob_show(CronShowAction(job_id=job.id), execution_context=_ctx("tenant-b"))
    assert res.error and "No such cron job" in res.error


@pytest.mark.asyncio
async def test_owner_turn_patch_keeps_every_other_rule(tmp_path, owner_turn):
    t = _tool(tmp_path)
    job = _job(t)
    res = await t.cronjob_edit(
        CronEditAction(job_id=job.id, old_text="Buy 0.1 ETH", new_text="Buy 0.05 ETH"),
        execution_context=_ctx())
    assert res.error is None and "task" in res.extracted_content
    task = t._cron_service.store.get(job.id).task
    assert "Buy 0.05 ETH" in task
    assert "reconcile the ledger before any swap" in task
    assert "write the ledger row before the report" in task
    stored = t._cron_service.store.get(job.id)
    assert stored.id == job.id and stored.payload["deliver"] == "telegram"


@pytest.mark.asyncio
@pytest.mark.parametrize("old,why", [("SAFETY", "2 times"), ("not in the task", "0 times")])
async def test_patch_must_match_exactly_once(tmp_path, owner_turn, old, why):
    t = _tool(tmp_path)
    job = _job(t)
    res = await t.cronjob_edit(CronEditAction(job_id=job.id, old_text=old, new_text="x"),
                               execution_context=_ctx())
    assert res.error and why in res.error
    assert t._cron_service.store.get(job.id).task == RULES


@pytest.mark.asyncio
async def test_agent_run_may_not_edit_an_owner_job(tmp_path, agent_turn):
    t = _tool(tmp_path)
    job = _job(t, authored_by="owner")
    res = await t.cronjob_edit(
        CronEditAction(job_id=job.id, old_text="Buy 0.1 ETH", new_text="Buy 9 ETH"),
        execution_context=_ctx())
    assert res.error and "scheduled by the owner" in res.error
    assert t._cron_service.store.get(job.id).task == RULES


@pytest.mark.asyncio
async def test_legacy_unstamped_job_counts_as_the_owners(tmp_path, agent_turn):
    t = _tool(tmp_path)
    job = t._cron_service.schedule(task=RULES, schedule_spec="1h", user_id="u1")
    res = await t.cronjob_edit(CronEditAction(job_id=job.id, schedule="2h"),
                               execution_context=_ctx())
    assert res.error and "scheduled by the owner" in res.error


@pytest.mark.asyncio
async def test_agent_run_may_edit_its_own_job(tmp_path, agent_turn):
    t = _tool(tmp_path)
    job = _job(t, authored_by="agent")
    res = await t.cronjob_edit(
        CronEditAction(job_id=job.id, schedule="2h", max_duration_seconds=900,
                       deliver="none", skills=["x-engagement"]),
        execution_context=_ctx())
    assert res.error is None
    stored = t._cron_service.store.get(job.id)
    assert stored.schedule_spec == "2h" and stored.max_duration_seconds == 900
    assert "deliver" not in stored.payload
    assert stored.payload["skills"] == ["x-engagement"]
    assert stored.payload["authored_by"] == "agent"


@pytest.mark.asyncio
async def test_agent_rig_past_the_ceiling_is_refused(tmp_path, agent_turn, monkeypatch):
    monkeypatch.setattr(gt, "allowed_self_goal_tools", lambda: gt._SELF_GOAL_ALLOWED_TOOLS)
    t = _tool(tmp_path)
    job = _job(t, authored_by="agent")
    res = await t.cronjob_edit(CronEditAction(job_id=job.id, rig="money_rail"),
                               execution_context=_ctx())
    assert res.error and "NOT granted" in res.error
    assert "rig" not in t._cron_service.store.get(job.id).payload


@pytest.mark.asyncio
async def test_bad_schedule_writes_nothing(tmp_path, owner_turn):
    """All-or-nothing: a refused schedule must not leave a half-applied task patch."""
    t = _tool(tmp_path)
    job = _job(t)
    res = await t.cronjob_edit(
        CronEditAction(job_id=job.id, old_text="Buy 0.1 ETH", new_text="Buy 0.05 ETH",
                       schedule="1s"),
        execution_context=_ctx())
    assert res.error and "minimum" in res.error
    assert t._cron_service.store.get(job.id).task == RULES


@pytest.mark.asyncio
async def test_cancelled_job_cannot_be_edited(tmp_path, owner_turn):
    t = _tool(tmp_path)
    job = _job(t)
    t._cron_service.cancel(job.id, user_id="u1")
    res = await t.cronjob_edit(CronEditAction(job_id=job.id, schedule="2h"),
                               execution_context=_ctx())
    assert res.error and "cancelled" in res.error


@pytest.mark.asyncio
async def test_forged_turn_is_refused(tmp_path, owner_turn):
    t = _tool(tmp_path)
    job = _job(t)
    t._self_scheduling_refusal = lambda ctx: "cronjob_schedule denied: forged"
    res = await t.cronjob_edit(CronEditAction(job_id=job.id, schedule="2h"),
                               execution_context=_ctx())
    assert res.error and "cronjob_edit denied" in res.error


def test_edit_records_an_audit_event(tmp_path, monkeypatch):
    from core.event_kinds import CRON_EDITED
    seen = []
    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    monkeypatch.setattr(svc, "_audit", lambda kind, **kw: seen.append((kind, kw)))
    job = svc.schedule(task=RULES, schedule_spec="1h", user_id="u1")
    seen.clear()
    assert svc.edit(job.id, user_id="u1", via="agent",
                    old_text="0.1", new_text="0.05") == ["task"]
    kind, kw = seen[0]
    assert kind == CRON_EDITED and kw["fields"] == ["task"]
    assert kw["old_text"] == "0.1" and kw["new_text"] == "0.05"


@pytest.mark.asyncio
async def test_unknown_delivery_target_is_refused(tmp_path, owner_turn):
    t = _tool(tmp_path)
    job = _job(t)
    res = await t.cronjob_edit(CronEditAction(job_id=job.id, deliver="carrier-pigeon"),
                               execution_context=_ctx())
    assert res.error and "unknown delivery target" in res.error
    assert t._cron_service.store.get(job.id).payload["deliver"] == "telegram"


# 2026-10-02: the cron-health-audit goal could not tell whether DATA PULL was
# pinned to z.ai (an unfunded seat) — show named the rig but never the provider.
@pytest.mark.asyncio
async def test_show_names_an_unpinned_job_as_the_owners_seat(tmp_path):
    t = _tool(tmp_path)
    job = _job(t)
    res = await t.cronjob_show(CronShowAction(job_id=job.id), execution_context=_ctx())
    assert "provider: (owner's seat)" in res.extracted_content


@pytest.mark.asyncio
async def test_show_names_a_pin_and_whether_it_can_serve(tmp_path, monkeypatch):
    import core.runtime_config as rc
    t = _tool(tmp_path)
    job = t._cron_service.schedule(task=RULES, schedule_spec="1h", user_id="u1",
                                   payload={"provider": "zai-coding", "model": "glm-5"})
    monkeypatch.setattr(rc, "_sentinel_active", lambda p=None: p == "zai-coding")
    monkeypatch.setattr(rc, "resolve_live_provider", lambda pref=None, env=None: "openrouter")
    res = await t.cronjob_show(CronShowAction(job_id=job.id), execution_context=_ctx())
    line = next(l for l in res.extracted_content.splitlines() if l.startswith("provider:"))
    assert "zai-coding" in line and "glm-5" in line
    assert "credit-dead" in line and "openrouter" in line


@pytest.mark.asyncio
async def test_show_a_live_pin_says_nothing_extra(tmp_path, monkeypatch):
    import core.runtime_config as rc
    t = _tool(tmp_path)
    job = t._cron_service.schedule(task=RULES, schedule_spec="1h", user_id="u1",
                                   payload={"provider": "openrouter"})
    monkeypatch.setattr(rc, "_sentinel_active", lambda p=None: False)
    res = await t.cronjob_show(CronShowAction(job_id=job.id), execution_context=_ctx())
    assert "provider: openrouter (pinned)" in res.extracted_content
    assert "credit-dead" not in res.extracted_content
