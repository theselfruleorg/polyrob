"""CO-F2 regression: deferred MCP actions from step N must actually execute
at step N+1, even when the MCP-throttle branch does not re-fire.

Bug: `_execute_actions` merges `self._deferred_mcp_actions` into a local
`actions_list` and clears the buffer, but execution further down reads
`actions_to_execute = model_output.action` — which is only reassigned when
the MCP-throttle branch re-fires. If it doesn't (e.g. the merged count is
within MAX_MCP_PER_STEP), the deferred actions are silently dropped: they
get validated/counted but never passed to `controller.multi_act`.
"""

import logging

import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_action(name: str):
    """A minimal stand-in for a Pydantic ActionModel: model_dump() exposes
    exactly one key (the action name), matching how _execute_actions reads
    `action_name = list(action_dump.keys())[0]`."""
    action = MagicMock(name=name)
    action.model_dump.return_value = {name: {}}
    return action


def _build_agent():
    from agents.task.agent.service import Agent

    a = object.__new__(Agent)
    a.logger = logging.getLogger("deferred-mcp-test")
    a.state = MagicMock()
    a.state.n_steps = 1
    a.state.total_actions_count = 0

    a.controller = MagicMock()
    a.controller.has_action.return_value = True
    a.controller.multi_act = AsyncMock(return_value=[])
    a.controller.registry.list_action_names.return_value = []

    # No validation-loop tracker => the blocked-MCP branch is skipped.
    a.tool_call_tracker = None

    a.message_manager = MagicMock()
    a.detect_action_loop = MagicMock(return_value=(False, None))
    a._build_execution_context = MagicMock(return_value=MagicMock())
    a._max_mcp_deferrals = 5

    return a


@pytest.mark.asyncio
async def test_deferred_mcp_action_executes_next_step_without_throttle_refire():
    """One MCP action was deferred last step; this step's model output has a
    single fresh non-MCP action. Since 1 deferred + 1 fresh = 1 MCP action
    total, the throttle does NOT re-fire (well under MAX_MCP_PER_STEP) — yet
    the deferred action must still reach controller.multi_act, ahead of the
    fresh one."""
    a = _build_agent()

    deferred = _make_action("mcp_execute_tool")
    a._deferred_mcp_actions = [deferred]

    fresh = _make_action("fresh_tool")
    model_output = MagicMock()
    model_output.action = [fresh]
    model_output.current_state = None

    await a._execute_actions(
        model_output=model_output,
        tool_calls_to_pass=None,
        state=a.state,
        step_info=None,
        browser_context=None,
    )

    a.controller.multi_act.assert_awaited_once()
    executed = a.controller.multi_act.call_args.kwargs["actions"]
    assert executed == [deferred, fresh]
    # The deferral buffer must be drained so the action isn't re-deferred forever.
    assert a._deferred_mcp_actions == []
