"""F16(ii) (063 WS-3) — the per-step state message is EPHEMERAL.

It used to be appended into ``history`` and then spliced back out mid-deque by
``remove_last_state_message()`` on every step. The splice rebuilt the whole deque
(``views.py`` converts to a list and back), invalidated the F6 provider-usage
anchor on EVERY step — the anchor fingerprints the covered prefix and the splice
changes it — and it put the browser screenshot into ``message_history.json``.

The one-shot ephemeral rail already does exactly what the state message needs:
ride the tail of ONE ``get_messages_for_llm()`` call and disappear. So the
invariant this file pins is simply:

    no state message is ever in ``history``

with ``STATE_MESSAGE_EPHEMERAL=false`` restoring the old append + splice.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.agent.views import AgentStepInfo
from agents.task.path import get_path_manager, pm, set_path_manager
from modules.llm.messages import HumanMessage
from tools.browser.views import BrowserState
from tools.dom.views import DOMElementNode

from tests.support.prefix_cache import serialize_messages


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


def _mm(session_id: str = "s-ephemeral") -> MessageManager:
	return MessageManager(
		llm=_FakeChatModel(),
		task="Test task",
		action_descriptions="Test actions",
		system_prompt_class=SystemPrompt,
		max_input_tokens=40000,
		image_tokens=800,
		session_id=session_id,
	)


@pytest.fixture()
def tmp_data_root(tmp_path):
	set_path_manager(get_path_manager(data_root=str(tmp_path)))
	yield tmp_path


def _add_state(mm: MessageManager, step: int = 0) -> None:
	mm.add_state_message(
		state=_minimal_state(),
		step_info=AgentStepInfo(step_number=step, max_steps=20),
		use_vision=False,
		include_browser_state=False,
	)


def _is_state(message) -> bool:
	return bool((getattr(message, "metadata", None) or {}).get("state_message"))


def _history_messages(mm: MessageManager) -> list:
	return [m.message for m in mm.history.messages]


# ---------------------------------------------------------------------------
# ON (default) — the state message never enters history
# ---------------------------------------------------------------------------


def test_no_state_message_ever_enters_history():
	mm = _mm()
	for step in range(3):
		_add_state(mm, step)
		assembled = mm.get_messages_for_llm()
		mm.commit_ephemeral_consumption()
		assert _is_state(assembled[-1]), "the state message must ride the tail"
		call_id = f"call_{step}"
		mm.add_tool_call_pair_atomic(
			ai_content=f"step {step}",
			tool_calls=[{"id": call_id, "name": "noop", "args": {}}],
			tool_responses=[(call_id, f"result {step}")],
		)
		mm.remove_last_state_message()  # the kept no-op call site in core/step.py

	assert not any(_is_state(m) for m in _history_messages(mm))
	assert len(mm.history.messages) == 6  # three AI+Tool pairs, nothing else


def test_the_state_message_rides_exactly_one_call():
	mm = _mm("s-ephemeral-once")
	_add_state(mm)
	first = mm.get_messages_for_llm()
	mm.commit_ephemeral_consumption()
	second = mm.get_messages_for_llm()

	assert _is_state(first[-1])
	assert not any(_is_state(m) for m in second)


def test_a_re_queued_stale_state_message_is_dropped():
	"""A transient invoke failure re-queues the ephemerals so a correspondent reply
	is never lost. The stale state message must not ride along beside the fresh
	one — the model would read two CURRENT STATE blocks."""
	mm = _mm("s-ephemeral-retry")
	mm.push_ephemeral_message(HumanMessage(content="a correspondent reply"))
	_add_state(mm, 0)
	mm.get_messages_for_llm()          # consumed into the pending buffer
	mm.restore_ephemeral_on_failure()  # the call failed: everything comes back

	_add_state(mm, 1)
	assembled = mm.get_messages_for_llm()
	states = [m for m in assembled if _is_state(m)]
	assert len(states) == 1
	assert "Current step: 2/20" in str(states[0].content)
	# The correspondent reply is NOT collateral damage.
	assert any("a correspondent reply" in str(m.content) for m in assembled)


def test_the_screenshot_never_reaches_message_history_json(tmp_data_root):
	mm = _mm("s-ephemeral-disk")
	state = _minimal_state()
	state.screenshot = "A" * 512
	state.url = "https://example.com"
	mm.add_state_message(
		state=state,
		step_info=AgentStepInfo(step_number=0, max_steps=20),
		use_vision=True,
		include_browser_state=False,
	)

	mm.save_to_disk("s-ephemeral-disk", "u_f16")
	path = pathlib.Path(pm().create_file_path(
		session_id="s-ephemeral-disk", subdir_name="memory",
		filename="message_history.json", user_id="u_f16"))
	saved = json.loads(path.read_text())

	# The durable conversation — the array that grows one entry per step and is
	# replayed on every restart — never carries a state message at all.
	assert not any((row.get("msg_metadata") or {}).get("state_message")
	               for row in saved["messages"])
	assert "AAAAAAAA" not in json.dumps(saved["messages"])
	# Until the call consumes it, it is an unconsumed one-shot like any other, so a
	# restart re-sends it rather than losing the turn.
	assert saved.get("pending_ephemeral")

	restored = _mm("s-ephemeral-disk")
	assert restored.load_from_disk("s-ephemeral-disk", "u_f16")
	assert any(_is_state(m) for m in restored._ephemeral_messages)
	assert not any(_is_state(m) for m in _history_messages(restored))

	# Once the call has taken it, it is gone from disk for good — under the old
	# append+splice every step's screenshot passed through the durable array.
	mm.get_messages_for_llm()
	mm.commit_ephemeral_consumption()
	mm.save_to_disk("s-ephemeral-disk", "u_f16")
	assert "AAAAAAAA" not in path.read_text()


# ---------------------------------------------------------------------------
# OFF — byte-identical to the append + splice it replaced
# ---------------------------------------------------------------------------


_CLOCK = re.compile(r"Current date and time: [0-9-]+ [0-9:]+")


def _normalised(messages) -> list:
	"""Serialized assembly with the wall clock masked (it ticks between runs)."""
	return [_CLOCK.sub("Current date and time: <t>", s)
	        for s in serialize_messages(messages)]


def test_flag_off_appends_into_history_and_the_splice_still_works(monkeypatch):
	monkeypatch.setenv("STATE_MESSAGE_EPHEMERAL", "false")
	mm = _mm("s-ephemeral-off")
	_add_state(mm)

	assert any(_is_state(m) for m in _history_messages(mm))
	mm.remove_last_state_message()
	assert not any(_is_state(m) for m in _history_messages(mm))


def test_flag_off_sends_the_same_bytes(monkeypatch):
	"""OFF must reproduce today's request exactly — that is the acceptance test."""
	monkeypatch.setenv("STATE_MESSAGE_EPHEMERAL", "false")
	off = _mm("s-ephemeral-bytes-off")
	_add_state(off)
	off_assembly = _normalised(off.get_messages_for_llm())

	monkeypatch.setenv("STATE_MESSAGE_EPHEMERAL", "true")
	on = _mm("s-ephemeral-bytes-on")
	_add_state(on)
	on_assembly = _normalised(on.get_messages_for_llm())

	assert on_assembly == off_assembly
