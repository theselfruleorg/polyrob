"""2026-08-18 (inbox HIGH item, 270 occurrences/6h): self.ActionModel/self.AgentOutput
are built ONCE at construction (construction.py) and bind every subsequent
model_output.action validation to that fixed field set. A mid-session
load_tool() call (progressive tool disclosure) registers new actions into the
controller's live registry but never refreshed these cached types — Pydantic's
default extra='ignore' then silently drops any dynamically-loaded tool's action
name from AgentOutput validation, producing a systematically empty
model_dump() for every subsequent call to that tool, misreported as "empty
action response" and feeding the thinking-loop/failure-counter escalation.

Confirmed live via a debug DIAG line: class_id == self.ActionModel_id (same
object — ruling out a class-generation mismatch), declared_field_count=49,
none of them the just-loaded code_execution_run_code action, while the
session's own memory said "code_execution loaded."
"""
import inspect
from typing import Optional

from pydantic import Field, create_model

from agents.task.agent.core.llm_runner import LLMRunnerMixin
from agents.task.agent.views import ActionModel, AgentBrain, AgentOutput


def _brain():
    return AgentBrain(
        page_summary="", memory="m", evaluation_previous_goal="e",
        next_goal="n", reasoning="r")


class _FakeController:
    """Minimal stand-in for Controller's create_action_model/get_action_names."""

    def __init__(self, action_names):
        self._action_names = list(action_names)

    def get_action_names(self):
        return list(self._action_names)

    def create_action_model(self):
        fields = {
            name: (Optional[dict], Field(default=None))
            for name in self._action_names
        }
        return create_model('ActionModel', __base__=ActionModel, **fields)

    def add_action(self, name):
        self._action_names.append(name)


class _Agent(LLMRunnerMixin):
    """Mirrors construction.py's ActionModel/AgentOutput binding exactly."""

    def __init__(self, controller):
        self.controller = controller
        self.ActionModel = controller.create_action_model()
        self.AgentOutput = AgentOutput.type_with_custom_actions(self.ActionModel)
        self._action_model_names = frozenset(controller.get_action_names())


def test_ensure_action_model_current_is_a_noop_when_unchanged():
    controller = _FakeController(["done", "send_message"])
    agent = _Agent(controller)
    original_action_model = agent.ActionModel
    original_agent_output = agent.AgentOutput

    agent._ensure_action_model_current()

    assert agent.ActionModel is original_action_model
    assert agent.AgentOutput is original_agent_output


def test_ensure_action_model_current_rebuilds_after_dynamic_tool_load():
    controller = _FakeController(["done", "send_message"])
    agent = _Agent(controller)

    # Simulate a mid-session load_tool('code_execution') call.
    controller.add_action("code_execution_run_code")
    agent._ensure_action_model_current()

    assert "code_execution_run_code" in agent.ActionModel.model_fields


def test_stale_action_model_silently_drops_a_dynamically_loaded_tool_call():
    """Reproduces the bug end-to-end WITHOUT the fix: a dict keyed by a tool
    that was loaded AFTER construction validates to an empty model when the
    session's AgentOutput binding is never refreshed."""
    controller = _FakeController(["done"])
    agent = _Agent(controller)
    controller.add_action("code_execution_run_code")
    # Deliberately do NOT call _ensure_action_model_current — this is the bug.

    action_dict = {"code_execution_run_code": {"code": "print(1)"}}
    parsed = agent.AgentOutput(current_state=_brain(), action=[action_dict])
    assert parsed.action[0].model_dump(exclude_unset=True) == {}, (
        "documents the pre-fix bug: a dynamically-loaded tool's action is "
        "silently dropped by the stale AgentOutput binding")


def test_fixed_action_model_correctly_validates_a_dynamically_loaded_tool_call():
    controller = _FakeController(["done"])
    agent = _Agent(controller)
    controller.add_action("code_execution_run_code")
    agent._ensure_action_model_current()  # THE FIX

    action_dict = {"code_execution_run_code": {"code": "print(1)"}}
    parsed = agent.AgentOutput(current_state=_brain(), action=[action_dict])
    assert parsed.action[0].model_dump(exclude_unset=True) == action_dict


def test_ensure_action_model_current_fails_open_on_controller_error():
    class _BrokenController(_FakeController):
        def get_action_names(self):
            raise RuntimeError("registry unavailable")

    agent = _Agent.__new__(_Agent)
    agent.controller = _BrokenController(["done"])
    agent.ActionModel = agent.controller.create_action_model()
    agent.AgentOutput = AgentOutput.type_with_custom_actions(agent.ActionModel)
    agent._action_model_names = frozenset(["done"])

    agent._ensure_action_model_current()  # must not raise


def test_get_next_action_calls_ensure_action_model_current_first():
    """Source-level wiring check: the refresh must happen before every LLM
    call, not just at construction — a full get_next_action() call is too
    heavyweight to exercise directly in a unit test."""
    src = inspect.getsource(LLMRunnerMixin.get_next_action)
    assert "_ensure_action_model_current" in src
