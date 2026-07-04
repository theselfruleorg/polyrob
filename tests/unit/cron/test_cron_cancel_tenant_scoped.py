"""E9 (A6 gap 6) — LIVE cross-tenant IDOR: any tenant could cancel another
tenant's cron job via cronjob_cancel, because CronJobStore.cancel/get took no
user_id filter. Mirrors the goal-cancel IDOR already fixed for
GoalBoard.cancel (agents/task/goals/board.py:401-413) — same optional-kwarg,
DB-filtered pattern.
"""
import types
import pytest

from cron.jobs import CronJobStore
from cron.service import CronService
from tools.cronjob_tools import CronJobTool, CronCancelAction


def _tool(tmp_path):
    t = object.__new__(CronJobTool)  # bypass BaseComponent.__init__ (matches test_cronjob_tool.py)
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return t


def _ctx(user):
    return types.SimpleNamespace(user_id=user)


# ── store layer ────────────────────────────────────────────────────────────

def test_store_cancel_denied_for_wrong_tenant(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    service = CronService(store)
    job = service.schedule(task="tenant a private recurring job", schedule_spec="1h", user_id="tenant-a")

    assert store.cancel(job.id, user_id="tenant-b") is False
    assert store.get(job.id).status == "scheduled"


def test_store_cancel_succeeds_for_owner(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    service = CronService(store)
    job = service.schedule(task="tenant a private recurring job", schedule_spec="1h", user_id="tenant-a")

    assert store.cancel(job.id, user_id="tenant-a") is True
    assert store.get(job.id).status == "cancelled"


def test_store_get_scoped_hides_other_tenants_job(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    service = CronService(store)
    job = service.schedule(task="tenant a private recurring job", schedule_spec="1h", user_id="tenant-a")

    assert store.get(job.id, user_id="tenant-b") is None
    assert store.get(job.id, user_id="tenant-a") is not None


# ── the real exploited surface: the agent tool ─────────────────────────────

@pytest.mark.asyncio
async def test_cronjob_cancel_tool_cross_tenant_denied(tmp_path):
    t = _tool(tmp_path)
    job = t._cron_service.schedule(
        task="tenant a's private recurring task", schedule_spec="1h", user_id="tenant-a",
    )

    result = await t.cronjob_cancel(CronCancelAction(job_id=job.id), execution_context=_ctx("tenant-b"))

    assert "No such cron job" in result.extracted_content, result.extracted_content
    assert t._cron_service.store.get(job.id).status == "scheduled"


@pytest.mark.asyncio
async def test_cronjob_cancel_tool_owner_still_works(tmp_path):
    t = _tool(tmp_path)
    job = t._cron_service.schedule(
        task="tenant a's own recurring task", schedule_spec="1h", user_id="tenant-a",
    )

    result = await t.cronjob_cancel(CronCancelAction(job_id=job.id), execution_context=_ctx("tenant-a"))

    assert "Cancelled cron job" in result.extracted_content
    assert t._cron_service.store.get(job.id).status == "cancelled"
