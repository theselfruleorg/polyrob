"""C3: a runtime nudge (planning note, empty-action / thinking-loop correction,
MCP block, verify-before-done) is control content — a non-user origin and a
non-user frame. A genuine user message (the HTTP API's default kind is
"guidance") keeps origin USER. The compaction static fallback counts only
origin==USER messages as user asks."""
from unittest.mock import MagicMock

import pytest

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.path import get_path_manager, set_path_manager
from modules.llm.messages import HumanMessage, MessageOrigin, make_control_message


@pytest.fixture()
def mm(tmp_path):
    set_path_manager(get_path_manager(data_root=str(tmp_path)))
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=40000,
        session_id="test-runtime-guidance")


def _tail(mm):
    return mm.history.messages[-1].message


def test_runtime_guidance_is_not_a_user_turn(mm):
    mm.inject_runtime_guidance("Call a function now.", source="empty_actions_validation")
    msg = _tail(mm)
    assert msg.origin == MessageOrigin.INTERVENTION
    assert "<system-directive>" in msg.content
    assert "User guidance" not in msg.content
    assert "not from the user" in msg.content


def test_runtime_guidance_never_forges_user_origin(mm):
    mm.inject_runtime_guidance("x", origin=MessageOrigin.USER)
    assert _tail(mm).origin == MessageOrigin.INTERVENTION


def test_planning_note_uses_guidance_origin(mm):
    mm.inject_runtime_guidance("Planning turn noted.", origin=MessageOrigin.GUIDANCE,
                               source="reasoning_turn_allowance")
    assert _tail(mm).origin == MessageOrigin.GUIDANCE


@pytest.mark.parametrize("kind", ["guidance", "comment", "correction", "warning"])
def test_genuine_user_guidance_keeps_user_origin(mm, kind):
    mm.inject_user_guidance([{"text": "please also check the logs", "kind": kind}])
    msg = _tail(mm)
    assert msg.origin == MessageOrigin.USER
    assert "please also check the logs" in msg.content


def test_static_fallback_counts_only_user_origin_as_asks(mm):
    msgs = [
        HumanMessage(content="Build the landing page", origin=MessageOrigin.USER),
        make_control_message("CALL A FUNCTION IN YOUR NEXT RESPONSE", MessageOrigin.INTERVENTION),
        make_control_message("Planning turn noted", MessageOrigin.GUIDANCE),
        make_control_message("wake up", MessageOrigin.SELF_WAKE),
    ]
    summary = mm._build_static_fallback_summary(msgs)
    active = summary.split("## Active Task\n", 1)[1].split("\n", 1)[0]
    assert active == "Build the landing page"
    assert "CALL A FUNCTION" not in summary
    assert "Planning turn noted" not in summary
    assert "wake up" not in summary


def test_step_execution_nudges_use_runtime_path():
    import inspect
    from agents.task.agent.core import run_loop, step_execution
    src = inspect.getsource(step_execution.StepExecutionMixin._validate_and_intervene)
    assert "inject_user_guidance" not in src
    assert "inject_user_guidance([{" not in inspect.getsource(step_execution)
    loop_src = inspect.getsource(run_loop.RunLoopMixin.run)
    i = loop_src.index("verify_before_done")
    assert "inject_runtime_guidance" in loop_src[i:i + 2500]
