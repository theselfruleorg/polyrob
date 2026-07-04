"""CX-H1: remove_last_state_message must remove by tag, never by shape.

Before the fix, remove_last_state_message scanned for a HumanMessage whose
content is a list OR whose string content contains 'Current url:'. A minimal
(non-browser) state message is a plain string WITHOUT that marker, so the scan
could walk past it and delete the nearest list-content HumanMessage instead —
which is exactly the shape of a user's multimodal image-guidance turn.
"""
import pytest

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.agent.views import AgentStepInfo
from modules.llm.messages import HumanMessage
from tools.dom.views import DOMElementNode
from tools.browser.views import BrowserState


FAKE_IMG = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


class _FakeChatModel:
	"""Minimal LLM stand-in — MessageManager only reads ``model_name`` off it."""

	def __init__(self, model_name: str = "gpt-5"):
		self.model_name = model_name


def _minimal_state() -> BrowserState:
	"""A BrowserState with no meaningful browser content (non-browser task)."""
	return BrowserState(
		url="",
		title="",
		element_tree=DOMElementNode(
			tag_name="body",
			attributes={},
			children=[],
			is_visible=True,
			parent=None,
			xpath="//body",
		),
		selector_map={},
		tabs=[],
	)


@pytest.fixture
def mm() -> MessageManager:
	return MessageManager(
		llm=_FakeChatModel(),
		task="Test task",
		action_descriptions="Test actions",
		system_prompt_class=SystemPrompt,
		max_input_tokens=4000,
		image_tokens=800,
	)


def test_remove_targets_tagged_state_not_user_image(mm: MessageManager):
	"""The image-guidance turn must survive; only the tagged state msg is removed."""
	mm.inject_user_guidance([
		{"text": "look at this", "kind": "comment", "metadata": {"image_attachments": [FAKE_IMG]}}
	])

	step_info = AgentStepInfo(step_number=0, max_steps=10)
	mm.add_state_message(
		state=_minimal_state(),
		step_info=step_info,
		use_vision=False,
		include_browser_state=False,
	)

	n = len(mm.get_messages())
	mm.remove_last_state_message()
	msgs = mm.get_messages()

	assert len(msgs) == n - 1
	assert any(isinstance(m.content, list) for m in msgs), "image turn must survive removal"


def test_remove_falls_back_to_shape_heuristic_for_untagged_legacy_history(mm: MessageManager):
	"""Histories loaded from disk that predate the tag still get cleaned up."""
	# Simulate a legacy (pre-tag) state message: plain HumanMessage with the old
	# marker string, added directly to history (bypassing add_state_message's tag).
	legacy_state_msg = HumanMessage(content="[CURRENT STATE]\nCurrent url: https://example.com\n")
	mm._add_message_with_tokens(legacy_state_msg)

	n = len(mm.get_messages())
	mm.remove_last_state_message()
	msgs = mm.get_messages()

	assert len(msgs) == n - 1
	assert legacy_state_msg not in msgs
