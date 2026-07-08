"""P0-2 / P0-5 regression tests (intelligence-polish plan 2026-07-07).

P0-2: the `_tool_call_id` PrivateAttr that `tool_calls_to_actions` stamps onto each
action instance is DROPPED by `model_dump(exclude_unset=True)` and by AgentOutput's
re-validation into fresh instances. The fix captures the ids before the dict
round-trip and re-stamps them onto `parsed.action` afterwards. Without the re-stamp,
`_pair_results_to_calls` never sees ids and falls to positional pairing every step.

P0-5: the post-retry "safe default" AgentOutput was constructed with a bare
`{"error": ...}` dict as `current_state`, but AgentBrain has three required fields —
so the "graceful degradation" actually raised ValidationError. The fix builds a valid
AgentBrain.
"""
import pytest
from pydantic import ValidationError

from tools.controller.registry.views import ActionModel
from agents.task.agent.views import AgentOutput, AgentBrain

# The runtime uses self.AgentOutput = AgentOutput.type_with_custom_actions(self.ActionModel)
# (construction.py:583), whose `action` field is `list[custom_actions]` — so a dict
# passed in is COERCED into an ActionModel instance (unlike the base AgentOutput, whose
# Union[Dict, ActionModel] keeps dicts as dicts). Build that variant here so the test
# exercises the same coercion the real path does.
CustomAgentOutput = AgentOutput.type_with_custom_actions(ActionModel)


def _valid_brain():
	return AgentBrain(
		evaluation_previous_goal="ok", memory="m", next_goal="n",
	)


# ---------------------------------------------------------------------------
# P0-2
# ---------------------------------------------------------------------------


def test_model_dump_drops_tool_call_id():
	"""Documents the bug: model_dump does not serialize the PrivateAttr, so an
	AgentOutput rebuilt from the dict has _tool_call_id=None."""
	a = ActionModel()
	a._tool_call_id = "call_A"
	dumped = a.model_dump(exclude_unset=True)
	assert "_tool_call_id" not in dumped
	rebuilt = CustomAgentOutput(current_state=_valid_brain(), action=[dumped])
	assert rebuilt.action[0]._tool_call_id is None  # the regression


def test_restamp_restores_tool_call_id_after_roundtrip():
	"""The fix: capture ids before the dict conversion, re-stamp onto parsed.action
	(built 1:1 in order), and identity is preserved."""
	actions = [ActionModel(), ActionModel()]
	actions[0]._tool_call_id = "call_A"
	actions[1]._tool_call_id = "call_B"

	action_tool_call_ids = [getattr(a, "_tool_call_id", None) for a in actions]
	action_list = [a.model_dump(exclude_unset=True) for a in actions]

	parsed = CustomAgentOutput(current_state=_valid_brain(), action=action_list)
	# re-stamp (mirrors next_action_internal)
	for act, tcid in zip(parsed.action, action_tool_call_ids):
		if tcid is not None:
			act._tool_call_id = tcid

	assert [a._tool_call_id for a in parsed.action] == ["call_A", "call_B"]


def test_restamp_order_survives_when_one_action_dropped():
	"""If a middle action fails validation and is dropped BEFORE this block, the
	ids list is built from the surviving actions in the same order — so the
	remaining calls still pair to the right results (the whole point of P0-2)."""
	# actions 0 and 2 survived; action 1 was dropped by tool_calls_to_actions.
	surviving = [ActionModel(), ActionModel()]
	surviving[0]._tool_call_id = "call_0"
	surviving[1]._tool_call_id = "call_2"
	ids = [getattr(a, "_tool_call_id", None) for a in surviving]
	action_list = [a.model_dump(exclude_unset=True) for a in surviving]
	parsed = CustomAgentOutput(current_state=_valid_brain(), action=action_list)
	for act, tcid in zip(parsed.action, ids):
		if tcid is not None:
			act._tool_call_id = tcid
	assert [a._tool_call_id for a in parsed.action] == ["call_0", "call_2"]


# ---------------------------------------------------------------------------
# P0-5
# ---------------------------------------------------------------------------


def test_bare_error_dict_brain_raises():
	"""Documents the bug: the old 'safe default' shape raises, defeating the
	graceful-degradation intent."""
	with pytest.raises(ValidationError):
		AgentOutput(
			current_state={"error": "parse_failed", "message": "x"},
			action=[],
		)


def test_valid_brain_safe_default_constructs():
	"""The fix shape constructs cleanly and carries the diagnostic in memory."""
	parsed = AgentOutput(
		current_state=AgentBrain(
			evaluation_previous_goal="Failed",
			memory="Previous LLM response could not be parsed after retries.",
			next_goal="Retry with a valid response.",
			reasoning="parse_failed",
		),
		action=[],
	)
	assert isinstance(parsed, AgentOutput)
	assert "could not be parsed" in parsed.current_state.memory
	assert parsed.action == []
