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

import pytest

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import AIMessage, HumanMessage

from tests.support.prefix_cache import common_prefix_len, serialize_messages


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
		return AIMessage(content="## Active Task\nsummarised")


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
	_fill(mm, turns=40)
	assert await mm.llm_compact_history()

	instruction = str(_summariser_request(llm)[-1].content)
	assert "EVERY message above this one" in instruction
	# The conversation is NOT inlined a second time.
	assert "conversation body conversation body" not in instruction


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


@pytest.mark.asyncio
async def test_flag_off_keeps_the_cold_shape(monkeypatch, tmp_path):
	monkeypatch.setenv("COMPACTION_WARM_PREFIX", "false")
	llm = _RecordingLLM()
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
	_fill(mm, turns=40)
	assert await mm.llm_compact_history()

	request = _summariser_request(llm)
	assert len(request) == 1 and isinstance(request[0], HumanMessage)
	assert "conversation body" in str(request[0].content)


# ---------------------------------------------------------------------------
# A provider that refuses the shape is learned once, not once per compaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_falls_back_and_is_remembered(tmp_path, caplog):
	llm = _RecordingLLM(fail_times=1)
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
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
