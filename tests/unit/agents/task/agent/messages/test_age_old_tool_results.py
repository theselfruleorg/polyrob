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
# 079 W7: a money verb's result keeps its full text for 24 h
# ---------------------------------------------------------------------------


def _named_pair(idx: int, name: str, content: str) -> list:
	call_id = f"call_{name}_{idx}"
	return [
		AIMessage(content=f"step {idx}", tool_calls=[{"id": call_id, "name": name, "args": {}}]),
		ToolMessage(content=content, tool_call_id=call_id),
	]


def _money_conversation() -> list:
	return ([HumanMessage(content="go")]
	        + _named_pair(0, "defi_trade_swap", f"SWAP RECEIPT\n{BIG}")
	        + _named_pair(1, "web_fetch", f"PAGE\n{BIG}")
	        + [AIMessage(content=f"filler {i}") for i in range(6)])


def test_a_young_money_result_keeps_its_full_text():
	from datetime import datetime, timedelta
	now = datetime(2026, 10, 13, 12, 0)
	messages = _money_conversation()
	stamps = [(now - timedelta(hours=2)).isoformat()] * len(messages)
	aged = age_old_tool_results(messages, keep_recent=6, timestamps=stamps, now=now)
	assert aged[2].content == messages[2].content, "a 2h-old swap receipt was aged"
	assert AGED_RESULT_MARKER in aged[4].content, "the non-money result must still age"


def test_an_old_money_result_ages_like_any_other():
	from datetime import datetime, timedelta
	now = datetime(2026, 10, 13, 12, 0)
	messages = _money_conversation()
	stamps = [(now - timedelta(hours=25)).isoformat()] * len(messages)
	aged = age_old_tool_results(messages, keep_recent=6, timestamps=stamps, now=now)
	assert AGED_RESULT_MARKER in aged[2].content
	assert aged[2].content.startswith("SWAP RECEIPT")


def test_a_money_result_with_no_timestamp_counts_as_young():
	messages = _money_conversation()
	aged = age_old_tool_results(messages, keep_recent=6)
	assert aged[2].content == messages[2].content


def test_money_is_the_capability_table_not_a_list():
	"""The predicate is the ``money`` dimension of core/tool_capabilities.py."""
	from core.money.classify import money_action
	from core.tool_capabilities import ids_with
	for tool in ids_with("money"):
		assert money_action(f"{tool}_anything")
	assert not money_action("web_fetch")


@pytest.mark.asyncio
async def test_the_message_manager_passes_the_stamps(tmp_path):
	"""Through the real MessageManager: the stamp on each ManagedMessage reaches the
	pass, so a fresh money result survives while a fresh web_fetch is aged."""
	mm = _mm(_NeverCalledLLM(), max_input_tokens=200_000)
	mm._compaction_checkpoint_dir = str(tmp_path)
	for msg in _money_conversation():
		mm._add_message_with_tokens(msg, _internal=True)
	assert mm.age_old_tool_results() > 0
	contents = [m.message.content for m in mm.history.messages]
	assert AGED_RESULT_MARKER not in contents[2]
	assert AGED_RESULT_MARKER in contents[4]


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
	# <= 5 conversation messages, plus (E-A1) the dated cut note and the owner's
	# latest message carried out of the cut head.
	assert len(mm.history.messages) <= 7
	assert any("[HISTORY CUT]" in str(m.message.content) for m in mm.history.messages)


def test_a_compaction_rebuild_keeps_each_message_timestamp():
	"""Release review: the rebuild re-added the kept tail with a fresh ``now``
	stamp, so a money receipt older than 24 h counted as young again and kept
	its full text forever. The rebuild and the emergency prune keep the stamp."""
	mm = _mm(_NeverCalledLLM(), max_input_tokens=200_000)
	for i in range(6):
		mm._add_message_with_tokens(HumanMessage(content=f"m{i}"), _internal=True)
	old = "2020-01-01T00:00:00"
	for m in mm.history.messages:
		m.metadata.timestamp = old
	tail = [m.message for m in list(mm.history.messages)[-4:]]
	mm._rebuild_with_summary("summary", 2, tail)
	kept = {m.message.content: m.metadata.timestamp for m in mm.history.messages}
	assert kept["m4"] == old and kept["m5"] == old

	mm.emergency_context_prune()
	assert [m.metadata.timestamp for m in mm.history.messages
	        if m.message.content in ("m4", "m5")] == [old, old]
