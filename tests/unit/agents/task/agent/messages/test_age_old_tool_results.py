"""F15 (063 WS-5) — deterministic ageing of old tool results.

Only compaction, the deque and dedup ever shrank history, so a 40 K-char
`web_fetch` from 30 steps ago rode every request verbatim — at cache-read price —
until the 85 % band fired the expensive LLM compaction (a reference agent runs five LLM-free
passes at that boundary; `context_compressor.py:3108-3139`).

`age_old_tool_results` demotes a long OLD tool result to its first line plus the
NAMED pointer the offload path already wrote. It runs only at a compaction
boundary, which is already a cold prefix, so it costs no extra cache miss — and
it runs FIRST, so if it frees enough the expensive step never happens.
"""

from __future__ import annotations

import pytest

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.messages.filters import (
	AGED_RESULT_MARKER,
	age_old_tool_results,
	tool_result_age_chars,
)
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import AIMessage, HumanMessage, ToolMessage

BIG = "payload " * 1000          # ~8000 chars
OFFLOAD_POINTER = 'Use the `read_file` action with file_path="result_7.json" to read the full content.'


def _offloaded(name: str = "result_7.json") -> str:
	return (
		"[LARGE CONTENT STORED]\n"
		f"File: {name}\n"
		"Size: 412,000 characters\n\n"
		f'HOW TO ACCESS: Use the `read_file` action with file_path="{name}" to read the full content.\n'
		f"PREVIEW (first 200 chars):\n{BIG}"
	)


def _pair(idx: int, content: str) -> list:
	call_id = f"call_{idx}"
	return [
		AIMessage(content=f"step {idx}", tool_calls=[{"id": call_id, "name": "web_fetch", "args": {}}]),
		ToolMessage(content=content, tool_call_id=call_id),
	]


def _conversation(pairs: int = 8) -> list:
	out: list = [HumanMessage(content="do the thing")]
	for i in range(pairs):
		out.extend(_pair(i, f"RESULT HEAD {i}\n{BIG}"))
	return out


# ---------------------------------------------------------------------------
# The pass itself
# ---------------------------------------------------------------------------


def test_old_results_are_demoted_and_the_tail_is_protected():
	messages = _conversation()
	aged = age_old_tool_results(messages, keep_recent=6)

	assert len(aged) == len(messages)
	tool_idx = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
	protected = len(messages) - 6
	for i in tool_idx:
		if i < protected:
			assert AGED_RESULT_MARKER in aged[i].content, f"message {i} was not aged"
			assert len(aged[i].content) < len(messages[i].content)
			assert aged[i].content.startswith("RESULT HEAD")
		else:
			assert aged[i].content == messages[i].content, "the protected tail was touched"


def test_the_pair_survives():
	"""Only the content shrinks — the ToolMessage keeps its place and its id, so
	no AIMessage(tool_calls) is ever left without its answer."""
	messages = _conversation()
	aged = age_old_tool_results(messages, keep_recent=6)

	for before, after in zip(messages, aged):
		assert type(before) is type(after)
		assert getattr(before, "tool_call_id", None) == getattr(after, "tool_call_id", None)
		assert getattr(before, "tool_calls", None) == getattr(after, "tool_calls", None)


def test_an_offload_pointer_is_kept_verbatim():
	"""The agent must be told the file name the offload path actually wrote — a
	second phrasing of the same pointer is how it learns to guess names."""
	messages = [HumanMessage(content="go")] + _pair(0, _offloaded()) + \
		[AIMessage(content=f"filler {i}") for i in range(6)]
	aged = age_old_tool_results(messages, keep_recent=6)

	body = aged[2].content
	assert OFFLOAD_POINTER in body
	assert AGED_RESULT_MARKER in body
	assert BIG not in body


def test_a_result_that_was_never_offloaded_says_so():
	messages = _conversation(pairs=1) + [AIMessage(content=f"filler {i}") for i in range(6)]
	aged = age_old_tool_results(messages, keep_recent=6)
	body = aged[2].content
	assert "never offloaded" in body and "re-run the tool" in body


def test_the_pass_is_idempotent():
	messages = _conversation()
	once = age_old_tool_results(messages, keep_recent=6)
	twice = age_old_tool_results(once, keep_recent=6)
	assert [m.content for m in once] == [m.content for m in twice]


def test_a_single_line_blob_is_idempotent_too():
	"""A minified 40k JSON answer has no second line — the surviving head is
	capped so the aged body still lands under the threshold."""
	blob = "{" + ("\"k\":\"v\"," * 4000) + "}"
	messages = [HumanMessage(content="go")] + _pair(0, blob) + \
		[AIMessage(content=f"filler {i}") for i in range(6)]
	once = age_old_tool_results(messages, keep_recent=6)
	twice = age_old_tool_results(once, keep_recent=6)
	assert len(once[2].content) < tool_result_age_chars()
	assert once[2].content == twice[2].content


def test_short_results_and_non_tool_messages_pass_through():
	messages = [HumanMessage(content="go")] + _pair(0, "ok, edited 3 files") + \
		[AIMessage(content="x" * 50_000)] + [AIMessage(content=f"f{i}") for i in range(6)]
	aged = age_old_tool_results(messages, keep_recent=6)
	assert [m.content for m in aged] == [m.content for m in messages]


def test_zero_disables_the_pass(monkeypatch):
	monkeypatch.setenv("TOOL_RESULT_AGE_CHARS", "0")
	messages = _conversation()
	assert [m.content for m in age_old_tool_results(messages)] == [m.content for m in messages]


# ---------------------------------------------------------------------------
# At the boundary: the cheap pass runs FIRST and can spare the expensive one
# ---------------------------------------------------------------------------


class _NeverCalledLLM:
	model_name = "gpt-5"

	async def ainvoke(self, messages, tools=None, **kwargs):  # pragma: no cover
		raise AssertionError("the LLM summariser ran even though ageing was enough")


def _mm(llm, max_input_tokens: int) -> MessageManager:
	return MessageManager(
		llm=llm,
		task="Test task",
		action_descriptions="Test actions",
		system_prompt_class=SystemPrompt,
		max_input_tokens=max_input_tokens,
		image_tokens=800,
		session_id="s-age",
	)


@pytest.fixture(autouse=True)
def _no_checkpoint(monkeypatch):
	monkeypatch.setenv("COMPACTION_CHECKPOINT", "false")
	monkeypatch.setenv("TASK_MAX_MESSAGES", "500")
	yield


@pytest.mark.asyncio
async def test_ageing_alone_can_spare_the_llm_summariser(tmp_path):
	llm = _NeverCalledLLM()
	mm = _mm(llm, max_input_tokens=200_000)
	mm._compaction_checkpoint_dir = str(tmp_path)
	for msg in _conversation(pairs=12):
		mm._add_message_with_tokens(msg, _internal=True)

	before = mm.history.total_tokens
	assert await mm.llm_compact_history()          # True: work WAS done
	assert mm.history.total_tokens < before
	# Nothing was summarised away: every turn is still there.
	assert len(mm.history.messages) == 25
	assert mm.last_compaction_savings() and mm.last_compaction_savings() > 0


@pytest.mark.asyncio
async def test_the_emergency_prune_keeps_the_conversation_when_ageing_is_enough(tmp_path):
	mm = _mm(_NeverCalledLLM(), max_input_tokens=200_000)
	mm._compaction_checkpoint_dir = str(tmp_path)
	for msg in _conversation(pairs=12):
		mm._add_message_with_tokens(msg, _internal=True)

	mm.emergency_context_prune()
	assert len(mm.history.messages) == 25, "the prune cut to 3 even though ageing was enough"


@pytest.mark.asyncio
async def test_the_prune_still_cuts_when_ageing_is_not_enough(tmp_path):
	"""The overflow safety net is not weakened: if ageing does not clear the band,
	the prune runs exactly as before."""
	mm = _mm(_NeverCalledLLM(), max_input_tokens=4000)
	mm._compaction_checkpoint_dir = str(tmp_path)
	for msg in _conversation(pairs=12):
		mm._add_message_with_tokens(msg, _internal=True)

	mm.emergency_context_prune()
	assert len(mm.history.messages) <= 5
