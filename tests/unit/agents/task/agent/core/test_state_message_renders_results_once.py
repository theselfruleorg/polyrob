"""F14 (063 WS-3) — a tool result reaches the model ONCE.

`result_processing._add_tool_messages` commits every step's results to history as
`ToolMessage`s (the native tool-calling path, which is the path every provider
POLYROB ships uses). The next step's state message then printed the SAME bytes
again as `Action result i/N: …`, so the uncached suffix of every tool step was
double-sized — and the second copy is the one that escaped
`TOOL_RESULT_MAX_TOKENS` in the 2026-09-20 incident (five anysite answers →
~110 K uncached tokens → a 221 s timeout with the cap "armed").

The legacy no-native-tools path keeps the render: there the results never became
ToolMessages, so it is their only appearance.
"""

from __future__ import annotations

from agents.task.agent.core.result_processing import _mark_results_committed
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.agent.views import AgentStepInfo
from tools.browser.views import BrowserState
from tools.controller.types import ActionResult
from tools.dom.views import DOMElementNode

from tests.support.prefix_cache import serialize_messages

PAYLOAD = "ANYSITE JSON ANSWER. " * 40


class _FakeChatModel:
	def __init__(self, model_name: str = "gpt-5"):
		self.model_name = model_name


def _minimal_state() -> BrowserState:
	return BrowserState(
		url="",
		title="",
		element_tree=DOMElementNode(
			tag_name="body", attributes={}, children=[], is_visible=True,
			parent=None, xpath="//body",
		),
		selector_map={},
		tabs=[],
	)


def _mm(session_id: str = "s-f14") -> MessageManager:
	return MessageManager(
		llm=_FakeChatModel(),
		task="Test task",
		action_descriptions="Test actions",
		system_prompt_class=SystemPrompt,
		max_input_tokens=40000,
		image_tokens=800,
		session_id=session_id,
	)


def _native_step(mm: MessageManager, results, *, call_id: str = "call_1",
                 content: str | None = None):
	"""Commit one native tool step exactly as the agent does."""
	calls = [{"id": call_id, "name": "web_fetch", "args": {}}]
	mm.add_tool_call_pair_atomic(
		ai_content="step 1",
		tool_calls=calls,
		tool_responses=[(call_id, content if content is not None else PAYLOAD)],
	)
	_mark_results_committed(mm, results, calls)


def _assemble(mm: MessageManager) -> str:
	return "\n".join(serialize_messages(mm.get_messages_for_llm(consume_ephemeral=False)))


def test_a_committed_result_appears_exactly_once(tmp_path):
	mm = _mm()
	results = [ActionResult(extracted_content=PAYLOAD, tool_call_id="call_1")]
	_native_step(mm, results)

	mm.add_state_message(
		state=_minimal_state(),
		result=results,
		step_info=AgentStepInfo(step_number=1, max_steps=20),
		use_vision=False,
		include_browser_state=False,
	)

	blob = _assemble(mm)
	assert blob.count(PAYLOAD) == 1, "the result bytes are in the request twice"
	assert "Action result 1/1" not in blob


def test_a_committed_error_is_not_re_rendered_either():
	"""`_pair_results_to_calls` pairs errors too — they are ToolMessages as well."""
	mm = _mm("s-f14-err")
	err = "MCP server refused: upstream 502 " + ("detail " * 30)
	results = [ActionResult(error=err, tool_call_id="call_1")]
	_native_step(mm, results, content=f"Error: {err}")

	mm.add_state_message(
		state=_minimal_state(),
		result=results,
		step_info=AgentStepInfo(step_number=1, max_steps=20),
		use_vision=False,
		include_browser_state=False,
	)

	blob = _assemble(mm)
	assert blob.count(err) == 1
	assert "Action error 1/1" not in blob


def test_an_uncommitted_result_list_still_renders():
	"""A fresh list (stop notice, budget halt, step-level exception) never became a
	ToolMessage, so the state message is its ONLY chance to reach the model."""
	mm = _mm("s-f14-fresh")
	committed = [ActionResult(extracted_content=PAYLOAD, tool_call_id="call_1")]
	_native_step(mm, committed)

	stop_notice = [ActionResult(
		extracted_content="Agent stopped by user. Send a message to continue.",
		include_in_memory=False,
	)]
	mm.add_state_message(
		state=_minimal_state(),
		result=stop_notice,
		step_info=AgentStepInfo(step_number=1, max_steps=20),
		use_vision=False,
		include_browser_state=False,
	)

	blob = _assemble(mm)
	assert "Action result 1/1: Agent stopped by user." in blob


def test_an_unpaired_result_leaves_the_list_unmarked():
	"""No tool_call_id ⇒ nothing was paired ⇒ the legacy render stands."""
	mm = _mm("s-f14-unpaired")
	results = [ActionResult(extracted_content=PAYLOAD)]
	_native_step(mm, results)
	assert getattr(mm, "_results_in_tool_messages", None) is None

	mm.add_state_message(
		state=_minimal_state(),
		result=results,
		step_info=AgentStepInfo(step_number=1, max_steps=20),
		use_vision=False,
		include_browser_state=False,
	)
	assert "Action result 1/1" in _assemble(mm)
