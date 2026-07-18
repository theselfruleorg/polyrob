"""P2/P3 (2026-07-02 architecture fix): a COMPLETED session only re-runs on input.

The "resume-to-check" model let anything re-run a completed interactive session
with no genuine queued input; the agent would burn an LLM call, conclude
"No new user input", and append another wall of no-op done-turns to the
persisted history (prod fa1212de: the tail was ~15 such turns, drowning real
owner questions). run_session must skip the run — without status churn and
without recreating an evicted orchestrator — when a completed session has no
pending input. Every legitimate resume path (STEER, continuation, self-wake,
delegation-result) queues its message BEFORE calling run_session.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.task_agent_lite import TaskAgent


def _task_agent():
    config = MagicMock()
    config.session_ttl_seconds = 3600
    config.max_sessions_in_memory = 10
    config.session_cleanup_interval = 60
    config.browser_headless = True
    agent = TaskAgent(container=MagicMock(), config=config)
    agent.task_available = True
    return agent


def _session_manager(status="completed"):
    manager = MagicMock()
    state = {"status": status}

    manager.get_session_info.side_effect = lambda sid: {
        "session_id": sid, "user_id": "u1", "status": state["status"],
        "request": {"task": "orig task"},
    }

    def try_transition(sid, frm, to):
        if state["status"] != frm:
            return False
        state["status"] = to
        return True

    manager.try_transition_status.side_effect = try_transition
    manager.update_session_status.side_effect = (
        lambda sid, status: state.__setitem__("status", status))
    manager._state = state
    return manager


def _orchestrator(queued=0, pending=None):
    inner_agent = MagicMock()
    inner_agent.agent_id = "executor_s1"
    inner_agent.hitl_manager = MagicMock()
    inner_agent.hitl_manager.get_queue_size.return_value = queued
    inner_agent.message_manager = MagicMock()
    # B1 (2026-07-13): the pending-input probe now also checks the ephemeral rail;
    # model the real contract (empty lists), not MagicMock's truthy auto-attrs.
    inner_agent.message_manager._ephemeral_messages = []
    inner_agent.message_manager._ephemeral_pending = []

    orch = MagicMock()
    orch.user_id = "u1"
    orch.session_id = "s1"
    orch.agents = {"executor_s1": inner_agent}
    orch._pending_messages = pending if pending is not None else []
    orch.execute_session = AsyncMock(return_value={
        "executor_s1": {"status": "completed"}})
    orch.cleanup = AsyncMock()
    return orch


@pytest.mark.asyncio
async def test_completed_session_with_no_input_skips_run():
    ta = _task_agent()
    ta.session_manager = _session_manager("completed")
    orch = _orchestrator(queued=0)
    ta._active_orchestrators["s1"] = orch

    result = await ta.run_session("u1", "s1")

    orch.execute_session.assert_not_awaited()
    assert ta.session_manager._state["status"] == "completed"  # no status churn
    assert "no new input" in result.lower() or "no pending input" in result.lower()


@pytest.mark.asyncio
async def test_completed_evicted_session_with_no_input_skips_without_recreate():
    ta = _task_agent()
    ta.session_manager = _session_manager("completed")
    ta._resolve_or_recreate = AsyncMock()  # must NOT be called

    await ta.run_session("u1", "s1")

    ta._resolve_or_recreate.assert_not_awaited()
    assert ta.session_manager._state["status"] == "completed"


@pytest.mark.asyncio
async def test_completed_session_with_queued_message_still_runs():
    ta = _task_agent()
    ta.session_manager = _session_manager("completed")
    orch = _orchestrator(queued=1)
    ta._active_orchestrators["s1"] = orch

    await ta.run_session("u1", "s1")

    orch.execute_session.assert_awaited()


@pytest.mark.asyncio
async def test_completed_session_with_pending_pre_agent_message_still_runs():
    ta = _task_agent()
    ta.session_manager = _session_manager("completed")
    orch = _orchestrator(queued=0, pending=[("hi", "comment", {})])
    ta._active_orchestrators["s1"] = orch

    await ta.run_session("u1", "s1")

    orch.execute_session.assert_awaited()


@pytest.mark.asyncio
async def test_fresh_created_session_unaffected_by_gate():
    ta = _task_agent()
    ta.session_manager = _session_manager("created")
    orch = _orchestrator(queued=0)
    ta._active_orchestrators["s1"] = orch
    ta._get_llm_for_request = AsyncMock(return_value=MagicMock())

    await ta.run_session("u1", "s1")

    orch.execute_session.assert_awaited()
