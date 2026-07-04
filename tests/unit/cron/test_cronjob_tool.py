"""P5d — cronjob tool glue: action metadata + delegation to CronService."""
import types

import pytest

from cron.jobs import CronJobStore
from cron.service import CronService
from tools.cronjob_tools import (
    CronJobTool, CronScheduleAction, CronListAction, CronCancelAction, cron_enabled,
)


def _tool(tmp_path):
    t = object.__new__(CronJobTool)  # bypass BaseComponent.__init__
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return t


def _ctx(user="u1"):
    return types.SimpleNamespace(user_id=user)


def test_actions_carry_decorator_metadata():
    # decorated => discoverable by the controller's get_actions()
    for name in ("cronjob_schedule", "cronjob_list", "cronjob_cancel"):
        fn = getattr(CronJobTool, name)
        assert hasattr(fn, "_description") and hasattr(fn, "_param_model")


@pytest.mark.asyncio
async def test_schedule_then_list_then_cancel(tmp_path):
    t = _tool(tmp_path)

    res = await t.cronjob_schedule(
        CronScheduleAction(task="check the deploy status", schedule="*/15 * * * *"),
        execution_context=_ctx(),
    )
    assert res.error is None and "Scheduled recurring" in res.extracted_content

    listed = await t.cronjob_list(CronListAction(), execution_context=_ctx())
    assert "check the deploy status"[:20] in listed.extracted_content

    # extract the job id from the listing and cancel it
    job = t._cron_service.list_jobs(user_id="u1")[0]
    cancelled = await t.cronjob_cancel(CronCancelAction(job_id=job.id), execution_context=_ctx())
    assert "Cancelled" in cancelled.extracted_content


@pytest.mark.asyncio
async def test_schedule_rejects_bad_spec(tmp_path):
    t = _tool(tmp_path)
    res = await t.cronjob_schedule(
        CronScheduleAction(task="do the thing later", schedule="not-a-schedule"),
        execution_context=_ctx(),
    )
    assert res.error and "Invalid schedule" in res.error


def test_max_duration_capped_at_180():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CronScheduleAction(task="x" * 20, schedule="1h", max_duration_seconds=600)


def test_cron_enabled_env(monkeypatch):
    monkeypatch.delenv("CRON_ENABLED", raising=False)
    assert cron_enabled() is False
    monkeypatch.setenv("CRON_ENABLED", "true")
    assert cron_enabled() is True


def test_register_cronjob_tool_gated_by_flag(monkeypatch):
    """UP-02: the tool must be reachable via get_tool_class only when cron is on.

    Cleans up TOOL_DESCRIPTORS so the module-global registry isn't left mutated.
    """
    from tools.cronjob_tools import register_cronjob_tool, CronJobTool
    from tools.descriptors import TOOL_DESCRIPTORS, TOOL_COMPONENTS, get_tool_class

    # Ensure a clean slate (other tests/imports may have force-registered it).
    TOOL_DESCRIPTORS.pop("cronjob", None)
    TOOL_COMPONENTS[:] = [(n, c) for n, c in TOOL_COMPONENTS if n != "cronjob"]
    try:
        # Flag off -> no-op, tool unreachable.
        monkeypatch.delenv("CRON_ENABLED", raising=False)
        assert register_cronjob_tool() is False
        assert get_tool_class("cronjob") is None

        # Flag on -> descriptor + class registered, tool reachable.
        monkeypatch.setenv("CRON_ENABLED", "true")
        assert register_cronjob_tool() is True
        assert get_tool_class("cronjob") is CronJobTool
    finally:
        TOOL_DESCRIPTORS.pop("cronjob", None)
        TOOL_COMPONENTS[:] = [(n, c) for n, c in TOOL_COMPONENTS if n != "cronjob"]
