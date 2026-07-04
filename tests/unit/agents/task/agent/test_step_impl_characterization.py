"""Characterization tests for Agent._step_impl (PR9 item #3 guard).

These lock in the OBSERVABLE behaviour of one step across the main branches so
the 1230-line _step_impl can be split into phases safely. A bare Agent
(object.__new__) is wired with mocked collaborators — no container, browser, or
LLM. Drives: get_next_action -> validate -> execute (controller.multi_act) ->
record (memory + history).

If these stay green before and after the _step_impl decomposition, the split
preserved behaviour on the covered paths.
"""

import logging

import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.task.agent.service import Agent
from agents.task.agent.views import ActionResult


# Instance attributes Agent.__init__ sets that _step_impl reads (defaults safe
# for a non-browser, native-tools step).
_STATE_DEFAULTS = dict(
    _is_sub_agent=False, _parent_session_id=None, _previous_actions=[],
    _last_browser_states=[], _action_repetition_counter=0, _last_action_count=0,
    _unchanged_state_count=0, _max_allowed_repetitions=3, _state_change_threshold=3,
    _deferred_mcp_actions=[], _max_mcp_deferrals=5, _llm_call_in_progress=False,
    _llm_call_start_time=None, agent_type="task", agent_id="agent_sess123",
    sensitive_data=None, available_file_paths=None, include_attributes=None,
    initial_actions=None, _skill_content=None, _profile_max_steps=None,
    _profile_system_message=None, _enabled_actions=None, _native_tools_debug=False,
    validate_output=False, generate_gif=False, save_conversation_path=None,
    tool_output_log_path=None, register_new_step_callback=None,
    register_done_callback=None,
)


def _build_agent(*, done=True, validate=True):
    a = object.__new__(Agent)
    a.logger = logging.getLogger("char")
    a._cancelled = False
    a.use_vision = False
    a._last_result = []
    a._last_model_output = None
    a.task = "do a thing"
    a.max_failures = 5
    a.max_actions_per_step = 10
    a.use_native_tools = True
    for k, v in _STATE_DEFAULTS.items():
        setattr(a, k, v)

    st = MagicMock()
    st.n_steps = 0
    st.stopped = False
    st.consecutive_failures = 0
    st.is_stuck_in_loop.return_value = False
    st.is_showing_loop_symptoms.return_value = False
    a.state = st

    a.task_context_manager = None
    a.tool_call_tracker = MagicMock()
    a.telemetry_manager = MagicMock()
    a.controller = MagicMock()
    a.controller.get_action_names.return_value = ["done"]
    a.controller.multi_act = AsyncMock(
        return_value=[ActionResult(is_done=done, extracted_content="Task completed", success=True)]
    )
    a.orchestrator = MagicMock()
    a.get_browser_context = AsyncMock(return_value=None)

    mm = MagicMock()
    mm.use_native_tools = True
    mm.get_context_usage_percent.return_value = 10.0
    mm.get_memory_usage_estimate.return_value = 1024
    mm.get_messages.return_value = []
    a.message_manager = mm

    a._log_context_breakdown = MagicMock()
    a.cleanup_memory = MagicMock()
    a._check_if_stopped = MagicMock()
    a._has_active_browser_usage = MagicMock(return_value=False)
    a._validate_model_output = MagicMock(return_value=validate)
    a._trigger_loop_intervention = MagicMock()
    a._save_step_to_memory = AsyncMock()
    a._make_history_item = AsyncMock()  # B-T5: now async (awaited in _step_impl)
    a._handle_large_action_results = MagicMock()

    mo = MagicMock()
    action = MagicMock()
    action.model_dump.return_value = {"done" if done else "click_element": {}}
    mo.action = [action]
    cs = MagicMock()
    cs.memory = "did it"
    mo.current_state = cs
    a.get_next_action = AsyncMock(return_value=mo)
    return a


@pytest.mark.asyncio
async def test_happy_done_step():
    """A successful step that returns a done action: LLM called, action executed,
    result recorded, no error."""
    a = _build_agent(done=True, validate=True)
    await a._step_impl()

    a.get_next_action.assert_awaited_once()
    a.controller.multi_act.assert_awaited_once()
    assert a.message_manager.add_state_message.called
    a._save_step_to_memory.assert_awaited()
    assert a._make_history_item.called
    assert a._last_result and a._last_result[0].is_done is True
    assert a._last_result[0].error is None


@pytest.mark.asyncio
async def test_non_done_step_executes_and_records():
    """A normal (non-terminal) action still executes and records without error."""
    a = _build_agent(done=False, validate=True)
    await a._step_impl()

    a.controller.multi_act.assert_awaited_once()
    assert a._last_result and a._last_result[0].is_done is False
    assert a._last_result[0].error is None
    assert a._make_history_item.called


@pytest.mark.asyncio
async def test_invalid_model_output_skips_execution():
    """When model output fails validation, the action is NOT executed and
    corrective guidance is injected."""
    a = _build_agent(done=True, validate=False)
    await a._step_impl()

    assert a._validate_model_output.called
    a.controller.multi_act.assert_not_awaited()
    assert a.message_manager.inject_user_guidance.called


@pytest.mark.asyncio
async def test_cancelled_before_start_raises():
    import asyncio
    a = _build_agent()
    a._cancelled = True
    with pytest.raises(asyncio.CancelledError):
        await a._step_impl()
    a.get_next_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_during_llm_is_not_swallowed_by_finally():
    """B1 (blocker): a CancelledError raised mid-step (task.cancel() / step timeout)
    must PROPAGATE out of _step_impl. The finally block's old `if not result: return`
    swallowed it, defeating cancellation + the step timeout. Guard the history write
    with a positive condition instead of an early return."""
    import asyncio
    a = _build_agent()
    a.get_next_action = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await a._step_impl()
    # history recording never happens on a cancelled step
    assert not a._make_history_item.called


@pytest.mark.asyncio
async def test_llm_error_is_handled_not_propagated():
    """If the LLM call raises, the step does NOT propagate it: the action is not
    executed and _handle_step_error runs (the main try's except path)."""
    a = _build_agent()
    a.get_next_action = AsyncMock(side_effect=RuntimeError("llm boom"))
    a._handle_step_error = AsyncMock(return_value=[])
    a._recover_from_error = AsyncMock()
    # must not raise
    await a._step_impl()
    a.controller.multi_act.assert_not_awaited()
    a._handle_step_error.assert_awaited_once()
