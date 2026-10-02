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
    """A genuine owner turn (M07: cronjob_schedule refuses any other)."""
    return types.SimpleNamespace(user_id=user, role="orchestrator",
                                 is_sub_agent=False, metadata={}, session_id=None)


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


def test_max_duration_capped_at_1800():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CronScheduleAction(task="x" * 20, schedule="1h", max_duration_seconds=1801)
    # 2026-09-18: a money rail needs ~15-25 min at 1-5 min/step; 1800 is allowed
    assert CronScheduleAction(task="x" * 20, schedule="1h",
                              max_duration_seconds=1800).max_duration_seconds == 1800


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


# --- M07 (security analysis 2026-09-23): self-scheduling limits -------------

@pytest.mark.asyncio
@pytest.mark.parametrize("ctx", [
    types.SimpleNamespace(user_id="u1"),  # role unset -> leaf
    types.SimpleNamespace(user_id="u1", role="orchestrator", is_sub_agent=True,
                          metadata={}, session_id=None),
    types.SimpleNamespace(user_id="u1", role="orchestrator", is_sub_agent=False,
                          metadata={"turn_kind": "self_wake"}, session_id=None),
])
async def test_forged_or_delegated_turn_cannot_schedule(tmp_path, ctx):
    t = _tool(tmp_path)
    res = await t.cronjob_schedule(
        CronScheduleAction(task="post an update every hour", schedule="1h"),
        execution_context=ctx)
    assert res.error and "genuine owner turn" in res.error
    assert t._cron_service.list_jobs(user_id="u1") == []


@pytest.mark.asyncio
async def test_autonomous_session_cannot_schedule(tmp_path):
    from agents.task.goals import autonomy_marker
    sid = "sess-autonomous-m07"
    autonomy_marker._SESSIONS[sid] = "goal-1"
    try:
        ctx = types.SimpleNamespace(user_id="u1", role="orchestrator",
                                    is_sub_agent=False, metadata={}, session_id=sid)
        t = _tool(tmp_path)
        res = await t.cronjob_schedule(
            CronScheduleAction(task="post an update every hour", schedule="1h"),
            execution_context=ctx)
        assert res.error and "genuine owner turn" in res.error
    finally:
        autonomy_marker._SESSIONS.pop(sid, None)


@pytest.mark.parametrize("spec", ["1s", "59s", "4m", "every 2m", "* * * * *",
                                  "*/2 * * * *", "0,1 * * * *"])
def test_minimum_interval_is_enforced_for_every_seat(tmp_path, spec):
    from cron.schedule import ScheduleError
    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    with pytest.raises(ScheduleError, match="minimum"):
        svc.schedule(task="too often", schedule_spec=spec, user_id="u1")


@pytest.mark.parametrize("spec", ["5m", "*/5 * * * *", "1h", "every day 09:00"])
def test_minimum_interval_allows_five_minutes_and_up(tmp_path, spec):
    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    assert svc.schedule(task="fine", schedule_spec=spec, user_id="u1")


@pytest.mark.asyncio
async def test_agent_job_cap_counts_only_self_scheduled_jobs(tmp_path):
    from cron.service import AGENT_MAX_ACTIVE_JOBS
    t = _tool(tmp_path)
    svc = t._cron_service
    # Owner-created jobs never count against the agent's cap.
    for i in range(3):
        svc.schedule(task=f"owner job {i}", schedule_spec="1h", user_id="u1")
    for i in range(AGENT_MAX_ACTIVE_JOBS):
        res = await t.cronjob_schedule(
            CronScheduleAction(task=f"agent job number {i}", schedule="1h"),
            execution_context=_ctx())
        assert res.error is None, res.error
    res = await t.cronjob_schedule(
        CronScheduleAction(task="one agent job too many", schedule="1h"),
        execution_context=_ctx())
    assert res.error and "cap" in res.error
    # Another tenant is unaffected.
    res = await t.cronjob_schedule(
        CronScheduleAction(task="other tenant job", schedule="1h"),
        execution_context=_ctx("u2"))
    assert res.error is None


def test_tenant_backstop_cap(tmp_path, monkeypatch):
    import cron.service as cs
    from cron.schedule import ScheduleError
    monkeypatch.setattr(cs, "MAX_ACTIVE_JOBS_PER_TENANT", 2)
    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    svc.schedule(task="a", schedule_spec="1h", user_id="u1")
    svc.schedule(task="b", schedule_spec="1h", user_id="u1")
    with pytest.raises(ScheduleError, match="cap"):
        svc.schedule(task="c", schedule_spec="1h", user_id="u1")
