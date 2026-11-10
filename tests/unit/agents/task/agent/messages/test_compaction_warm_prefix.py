"""F11 (063 WS-5) — the compaction summariser is a WARM-prefix call.

`_run_summarization` sent a lone `HumanMessage` carrying the whole middle as
TEXT, to `aux_llm or self.llm`. When that is the session's own model the request
is completely cold even though the provider is holding those exact bytes from the
step call a moment earlier — the middle is re-read at full price (review row 7,
DSH `region.ts:532-563`).

The warm shape is `[foundation + the messages being summarised + ONE trailing
instruction]`: everything but the instruction is the cached prefix. A DIFFERENT
model cannot hit that prefix, so an aux model always keeps the old shape.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.messages import compactor as C
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import AIMessage, HumanMessage

from tests.support.prefix_cache import (
	common_prefix_len, serialize_messages, wire_adapter, wire_prefix_len, wire_rows,
)

TOOLS = [{"type": "function", "function": {"name": "done", "parameters": {}}},
         {"type": "function", "function": {"name": "noop", "parameters": {}}}]
COMPLETE = "## Active Task\nsummarised\n## End of summary"


class _RecordingLLM:
	"""Records every `ainvoke` request; answers with a usable summary."""

	def __init__(self, model_name: str = "gpt-5", fail_times: int = 0):
		self.model_name = model_name
		self.requests: list = []
		self.fail_times = fail_times

	async def ainvoke(self, messages, tools=None, **kwargs):
		self.requests.append(list(messages))
		if self.fail_times > 0:
			self.fail_times -= 1
			raise ValueError("messages.1: requests with tool_result must define tools")
		return AIMessage(content="## Active Task\nsummarised\n## End of summary")


def _mm(llm, session_id="s-warm", max_input_tokens=4000) -> MessageManager:
	return MessageManager(
		llm=llm,
		task="Test task",
		action_descriptions="Test actions",
		system_prompt_class=SystemPrompt,
		max_input_tokens=max_input_tokens,
		image_tokens=800,
		session_id=session_id,
	)


def _fill(mm: MessageManager, turns: int = 200) -> None:
	for i in range(turns):
		body = f"turn {i}: " + ("conversation body " * 12)
		mm._add_message_with_tokens(
			HumanMessage(content=body) if i % 2 == 0 else AIMessage(content=body),
			_internal=True,
		)


@pytest.fixture(autouse=True)
def _roomy_deque(monkeypatch, tmp_path):
	monkeypatch.setenv("TASK_MAX_MESSAGES", "500")
	monkeypatch.setenv("COMPACTION_CHECKPOINT", "false")
	yield


def _summariser_request(llm: _RecordingLLM) -> list:
	assert llm.requests, "the summariser never called the model"
	return llm.requests[-1]


# ---------------------------------------------------------------------------
# ON (default), no aux model — the request extends the step request's prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_summariser_shares_the_step_requests_prefix(tmp_path):
	llm = _RecordingLLM()
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
	mm.warm_summary_tools = TOOLS
	_fill(mm)

	previous = serialize_messages(mm.get_messages_for_llm(consume_ephemeral=False))
	assert await mm.llm_compact_history()

	request = serialize_messages(_summariser_request(llm))
	shared = common_prefix_len(previous, request)
	assert shared == len(request) - 1, (
		"only the trailing instruction may be new bytes; "
		f"kept {shared} of {len(request) - 1}")
	assert shared / len(previous) > 0.9, (
		f"warm prefix covered only {shared}/{len(previous)} of the step request")
	assert "STRUCTURED SUMMARY:" in str(_summariser_request(llm)[-1].content)


@pytest.mark.asyncio
async def test_the_instruction_points_at_the_messages_instead_of_repeating_them(tmp_path):
	llm = _RecordingLLM()
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
	mm.warm_summary_tools = TOOLS
	_fill(mm, turns=40)
	assert await mm.llm_compact_history()

	instruction = str(_summariser_request(llm)[-1].content)
	assert "EVERY message above this one" in instruction
	# The conversation is NOT inlined a second time.
	assert "conversation body conversation body" not in instruction


# ---------------------------------------------------------------------------
# 106 C2: the WIRE request, through the real adapter and client
# ---------------------------------------------------------------------------


def _tool_turns(mm, turns: int, start: int = 0) -> None:
	for i in range(start, start + turns):
		call_id = f"call_{i}"
		mm.add_tool_call_pair_atomic(
			ai_content=f"step {i}",
			tool_calls=[{"id": call_id, "name": "noop", "args": {"i": i}}],
			tool_responses=[(call_id, f"result {i}: " + ("detail " * 40))],
		)


async def _step_and_compact(mm, adapter):
	"""One step call, then one compaction. Returns (step request, summariser request)."""
	await adapter.ainvoke(mm.get_messages_for_llm(consume_ephemeral=False), tools=TOOLS)
	step = adapter.wire_calls()[-1]
	mm.warm_summary_tools = TOOLS
	assert await mm.llm_compact_history()
	return step, adapter.wire_calls()[-1]


@pytest.mark.asyncio
async def test_on_the_wire_only_the_instruction_is_new(tmp_path):
	adapter = wire_adapter(text=COMPLETE)
	mm = _mm(adapter, max_input_tokens=4000)
	mm._compaction_checkpoint_dir = str(tmp_path)
	_tool_turns(mm, 30)

	step, summary = await _step_and_compact(mm, adapter)

	assert wire_rows(summary)[0] == wire_rows(step)[0], "tools[] differ"
	assert wire_prefix_len(step, summary) == len(wire_rows(summary)) - 1


@pytest.mark.asyncio
async def test_the_second_compaction_keeps_the_prior_summary_in_the_prefix(tmp_path):
	"""The prior summary left the request, so from the second compaction on the
	"warm" call shared the foundation only."""
	adapter = wire_adapter(text=COMPLETE)
	mm = _mm(adapter, max_input_tokens=4000)
	mm._compaction_checkpoint_dir = str(tmp_path)
	_tool_turns(mm, 30)
	await _step_and_compact(mm, adapter)
	mm._compaction_savings = []
	_tool_turns(mm, 30, start=30)

	step, summary = await _step_and_compact(mm, adapter)

	assert "<compacted-history>" in str(step["messages"]) or "COMPACTED HISTORY" in str(step["messages"])
	assert wire_prefix_len(step, summary) == len(wire_rows(summary)) - 1


@pytest.mark.asyncio
async def test_a_repeated_tool_result_does_not_cut_the_prefix(tmp_path):
	"""The step call dedups a repeated result, so the summariser must too."""
	adapter = wire_adapter(text=COMPLETE)
	mm = _mm(adapter, max_input_tokens=4000)
	mm._compaction_checkpoint_dir = str(tmp_path)
	same = "IDENTICAL TOOL OUTPUT. " * 12
	for i in range(30):
		mm.add_tool_call_pair_atomic(
			ai_content=f"step {i}",
			tool_calls=[{"id": f"c{i}", "name": "noop", "args": {"i": i}}],
			tool_responses=[(f"c{i}", same)],
		)
	step, summary = await _step_and_compact(mm, adapter)
	assert wire_prefix_len(step, summary) == len(wire_rows(summary)) - 1


@pytest.mark.asyncio
async def test_the_summariser_reads_the_bytes_ageing_is_about_to_cut(tmp_path):
	"""106 C3 x C2: ageing rewrites the history, and the provider holds the old bytes."""
	adapter = wire_adapter(text=COMPLETE)
	mm = _mm(adapter, max_input_tokens=4000)
	mm._compaction_checkpoint_dir = str(tmp_path)
	for i in range(30):
		mm.add_tool_call_pair_atomic(
			ai_content=f"step {i}",
			tool_calls=[{"id": f"c{i}", "name": "noop", "args": {"i": i}}],
			tool_responses=[(f"c{i}", f"long result {i} " + ("detail " * 400))],
		)
	step, summary = await _step_and_compact(mm, adapter)
	assert mm.compaction_rewrote_history is True
	assert wire_prefix_len(step, summary) == len(wire_rows(summary)) - 1


@pytest.mark.asyncio
async def test_the_summariser_asks_for_no_tool_call(tmp_path):
	"""``tools[]`` holds ``done``, so with no explicit choice the client asks for
	``required`` and the model cannot write the summary as text."""
	adapter = wire_adapter(text=COMPLETE)
	mm = _mm(adapter, max_input_tokens=4000)
	mm._compaction_checkpoint_dir = str(tmp_path)
	_tool_turns(mm, 30)
	step, summary = await _step_and_compact(mm, adapter)
	assert step["tool_choice"] == "required"
	assert summary["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_a_span_over_the_input_budget_at_its_real_size_goes_cold(tmp_path, monkeypatch):
	"""The windows measure CAPPED sizes, and the warm shape caps nothing."""
	monkeypatch.setattr(C, "_SUMMARIZE_INPUT_BUDGET_CHARS", 20_000)
	llm = _RecordingLLM()
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
	mm.warm_summary_tools = TOOLS
	_fill(mm, turns=20)
	for i in range(6):
		mm._add_message_with_tokens(HumanMessage(content="huge " * 2000), _internal=True)
	_fill(mm, turns=20)
	assert await mm.llm_compact_history()
	assert all(len(r) == 1 for r in llm.requests), "an over-budget span rode the warm shape"


@pytest.mark.asyncio
async def test_a_session_with_tools_that_cannot_name_them_goes_cold(tmp_path):
	llm = _RecordingLLM()
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
	_fill(mm, turns=40)
	assert await mm.llm_compact_history()
	assert len(_summariser_request(llm)) == 1


# ---------------------------------------------------------------------------
# 106 R9: a stalled summariser is bounded, and reads as transient
# ---------------------------------------------------------------------------


class _StalledLLM(_RecordingLLM):
	async def ainvoke(self, messages, tools=None, **kwargs):
		self.requests.append(list(messages))
		await asyncio.sleep(30)


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [True, False])
async def test_a_stalled_summariser_keeps_the_full_context(tmp_path, monkeypatch, warm):
	monkeypatch.setattr(MessageManager, "calculate_llm_timeout", lambda self, *a, **k: 0.01)
	llm = _StalledLLM()
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
	mm.warm_summary_tools = TOOLS if warm else None
	_fill(mm, turns=40)
	before = len(mm.history.messages)
	assert await mm.llm_compact_history() is False
	assert len(mm.history.messages) == before
	assert not getattr(llm, C._WARM_UNAVAILABLE_ATTR, False), "a stall says nothing about the shape"


# ---------------------------------------------------------------------------
# The two cases that must keep today's shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_aux_model_keeps_the_cold_shape(tmp_path):
	"""A different model has a different cache entry — the warm shape would only
	add bytes."""
	llm = _RecordingLLM()
	aux = _RecordingLLM("cheap-aux")
	mm = _mm(llm)
	mm.aux_llm = aux
	mm._compaction_checkpoint_dir = str(tmp_path)
	_fill(mm, turns=40)
	assert await mm.llm_compact_history()

	assert not llm.requests, "the session model was called for a summary"
	request = _summariser_request(aux)
	assert len(request) == 1 and isinstance(request[0], HumanMessage)
	assert "Conversation to summarize:" in str(request[0].content)
	assert "conversation body" in str(request[0].content)


# ---------------------------------------------------------------------------
# A provider that refuses the shape is learned once, not once per compaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_falls_back_and_is_remembered(tmp_path, caplog):
	llm = _RecordingLLM(fail_times=1)
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
	mm.warm_summary_tools = TOOLS
	_fill(mm, turns=40)

	assert await mm.llm_compact_history()
	# Two calls: the refused warm one, then the cold one that carried the answer.
	assert len(llm.requests) == 2
	assert len(llm.requests[0]) > 1            # warm
	assert len(llm.requests[1]) == 1           # cold fallback
	assert "conversation body" in str(llm.requests[1][0].content)

	# The refusal is a property of the CLIENT: the next compaction goes straight
	# to the cold shape instead of probing again.
	mm._compaction_savings = []
	_fill(mm, turns=40)
	assert await mm.llm_compact_history()
	assert len(llm.requests) == 3
	assert len(llm.requests[2]) == 1


@pytest.mark.asyncio
async def test_a_client_that_ignores_none_is_read_as_a_refusal(tmp_path):
	"""A client that does not honor ``tool_choice: none`` answers with a tool call and
	no text. That is no summary, and an empty one would throw the summarised span away."""
	class _ToolCaller(_RecordingLLM):
		async def ainvoke(self, messages, tools=None, **kwargs):
			self.requests.append(list(messages))
			if tools:
				return AIMessage(content="", tool_calls=[{"id": "c", "name": "done", "args": {}}])
			return AIMessage(content="## Active Task\nsummarised\n## End of summary")

	llm = _ToolCaller()
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
	mm.warm_summary_tools = TOOLS
	_fill(mm, turns=40)
	assert await mm.llm_compact_history()
	assert [len(r) > 1 for r in llm.requests] == [True, False]
	assert getattr(llm, C._WARM_UNAVAILABLE_ATTR, False)
	assert "summarised" in str(list(mm.history.messages)[0].message.content)


# ---------------------------------------------------------------------------
# A refusal of the CONTENT is not a refusal of the shape: it is never re-sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [True, False])
async def test_a_refused_summary_takes_the_static_fallback_with_no_second_call(
		tmp_path, caplog, warm):
	import logging
	from core.exceptions import LLMRefusalError

	class _Refuser(_RecordingLLM):
		async def ainvoke(self, messages, tools=None, **kwargs):
			self.requests.append(list(messages))
			raise LLMRefusalError("provider: stopped", category="test_category", kind="filtered")

	llm = _Refuser()
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
	mm.warm_summary_tools = TOOLS if warm else None
	_fill(mm, turns=40)
	with caplog.at_level(logging.WARNING):
		assert await mm.llm_compact_history()
	assert len(llm.requests) == 1                       # never re-sent cold
	assert not getattr(llm, C._WARM_UNAVAILABLE_ATTR, False)   # the shape is fine
	report = mm.last_summary_report or {}
	assert "safety filter" in report.get("refused", "")
	assert "test_category" in report["refused"]
	assert any("safety filter" in r.getMessage() for r in caplog.records)
	assert "static fallback" in str(list(mm.history.messages)[0].message.content)
