"""Task 13: Early-exit hygiene (CO-F3/F4/F8).

CO-F3: the two normal early returns in `_step_impl` (validate-and-intervene
False; process-action-results None) must clean up the state message that was
already added to history, same as the exception handlers already do.

CO-F4: a placeholder brain ("Synthesis pending - will be generated after
actions execute") must never be persisted to H-MEM as a real finding — it
should fall into the same fallback path as an empty/too-short finding.

CO-F8: after `_process_action_results` synthesizes a brain state (no-text
providers), `_last_brain_state` must be updated to the synthesized brain so
the next recall query is enriched instead of degrading to the static task
string.
"""

import logging

import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.task.agent.service import Agent
from agents.task.agent.views import ActionResult


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


def _build_agent(*, validate=True, process_result=AsyncMock()):
    a = object.__new__(Agent)
    a.logger = logging.getLogger("early-exit-hygiene")
    a._cancelled = False
    a.use_vision = False
    a._last_result = []
    a._last_model_output = None
    a._last_brain_state = None
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
        return_value=[ActionResult(is_done=False, extracted_content="ok", success=True)]
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
    a._make_history_item = AsyncMock()
    a._handle_large_action_results = MagicMock()
    a._process_action_results = process_result

    mo = MagicMock()
    action = MagicMock()
    action.model_dump.return_value = {"click_element": {}}
    mo.action = [action]
    cs = MagicMock()
    cs.memory = "did it"
    mo.current_state = cs
    a.get_next_action = AsyncMock(return_value=mo)
    return a


@pytest.mark.asyncio
async def test_planning_turn_early_return_removes_state_message():
    """(a) _validate_and_intervene -> False must clean up the state message
    it just added, same as the other early-exit paths."""
    a = _build_agent(validate=False)
    await a._step_impl()

    a.message_manager.remove_last_state_message.assert_called()
    # Never got far enough to process/record actions.
    a.controller.multi_act.assert_not_awaited()


@pytest.mark.asyncio
async def test_corrupted_state_early_return_removes_state_message():
    """(a) _process_action_results -> None (corrupted tool-message pairing)
    must also clean up the state message."""
    a = _build_agent(validate=True, process_result=AsyncMock(return_value=None))
    await a._step_impl()

    a.message_manager.remove_last_state_message.assert_called()
    assert not a._make_history_item.called


class _FakeMemoryWriter:
    """Minimal stand-in exercising the real _save_step_to_memory fallback logic."""

    from agents.task.agent.core.memory_writer import MemoryWriterMixin

    _save_step_to_memory = MemoryWriterMixin._save_step_to_memory
    _build_action_summary = MemoryWriterMixin._build_action_summary
    _extract_finding_from_results = MemoryWriterMixin._extract_finding_from_results


@pytest.mark.asyncio
async def test_placeholder_brain_never_lands_in_hmem():
    """(b) A brain memory field starting with 'Synthesis pending' must be
    treated as empty and fall through to the fallback finding, not be stored
    verbatim as the H-MEM finding."""
    w = _FakeMemoryWriter()
    w.logger = logging.getLogger("hmem-test")
    w.session_id = "sess123"
    w.state = MagicMock()
    w.task_context_manager = MagicMock()
    w.task_context_manager.add_step_memory = MagicMock(return_value=True)
    w.task_context_manager.get_session = MagicMock(return_value=None)
    w.message_manager = MagicMock()

    brain_state = {
        "memory": "Synthesis pending - will be generated after actions execute",
        "next_goal": "keep going",
    }

    await w._save_step_to_memory(
        step_number=1,
        brain_state=brain_state,
        actions=[],
        results=[],
        step_info=None,
    )

    assert w.task_context_manager.add_step_memory.call_count == 1
    _, kwargs = w.task_context_manager.add_step_memory.call_args
    finding_text = kwargs.get("finding")
    assert finding_text is not None
    assert "Synthesis pending" not in finding_text
    # Fell through to the next_goal fallback (results empty, memory rejected).
    assert finding_text == "keep going"


@pytest.mark.asyncio
async def test_placeholder_brain_does_not_trip_thinking_loop_or_set_last_finding():
    """(A1) Two consecutive 'Synthesis pending' placeholder steps must not trip
    the identical-memory thinking-loop warning and must never set
    _last_memory_finding to the placeholder (it is treated as empty)."""
    w = _FakeMemoryWriter()
    w.logger = logging.getLogger("hmem-a1-test")
    w.session_id = "sess123"
    w.state = MagicMock()
    w.task_context_manager = MagicMock()
    w.task_context_manager.add_step_memory = MagicMock(return_value=True)
    w.task_context_manager.get_session = MagicMock(return_value=None)
    w.message_manager = MagicMock()

    brain_state = {
        "memory": "Synthesis pending - will be generated after actions execute",
        "next_goal": "keep going",
    }

    for step in (1, 2):
        await w._save_step_to_memory(
            step_number=step, brain_state=brain_state,
            actions=[], results=[], step_info=None,
        )

    # Never stored the placeholder as _last_memory_finding.
    assert getattr(w, "_last_memory_finding", None) is None
    # No thinking-loop warning was pushed (message_manager.push_ephemeral_message
    # is the warning path — it must not have been called for placeholders).
    w.message_manager.push_ephemeral_message.assert_not_called()


@pytest.mark.asyncio
async def test_last_brain_state_updated_after_synthesis():
    """(c) After _process_action_results synthesizes a brain state (no text
    content from the provider), _last_brain_state must reflect it so the next
    recall query is enriched."""
    from agents.task.agent.core.result_processing import ResultProcessingMixin

    a = object.__new__(Agent)
    a.logger = logging.getLogger("brain-writeback-test")
    a.state = MagicMock()
    a.state.n_steps = 3
    a._last_model_output = None
    a._last_brain_state = None
    a._log_tool_outputs = MagicMock()
    a._handle_large_action_results = MagicMock()
    a._add_tool_messages = AsyncMock(return_value=True)
    a._build_memory_from_actions = MagicMock(return_value="synthesized memory")
    a._extract_progress_from_memory = MagicMock(return_value=None)

    mo = MagicMock()
    action = MagicMock()
    action.model_dump.return_value = {"click_element": {"x": 1}}
    mo.action = [action]
    mo.current_state = None  # No text content -> triggers synthesis

    result = [ActionResult(is_done=False, extracted_content="clicked", success=True)]

    out = await ResultProcessingMixin._process_action_results(a, result, mo, tool_calls_to_pass=[])

    assert out is result
    assert mo.current_state is not None
    assert a._last_brain_state is mo.current_state
