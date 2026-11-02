"""F8 (prod review 2026-10-29): the "LLM compaction cut 1%" in a long owner chat.

Session 82b74d81 (10-28) sat at 85-98 % of its history budget for hours. The
journal shows two defects, not a full prefix:

1. The cheap ageing pass "was enough" when it moved the gauge from 85.5 % to
   84.7 %. The LLM summariser never ran, the step ladder stamped its cooldown
   for that 1 % cut, and the next step was back over 85 %.
2. The cooldown stamp is a step number, and the step counter restarts every
   run (every owner turn). A stamp from an earlier turn read "compacted -7
   step(s) ago" and held the cooldown in the next turn.
"""
import pytest
from unittest.mock import MagicMock

from agents.task.agent.messages.compactor import AGEING_ENOUGH_PCT
from agents.task.agent.prompts import SystemPrompt
from agents.task.agent.message_manager.service import MessageManager
from modules.llm.messages import AIMessage, HumanMessage, ToolMessage

BIG = "payload " * 1000


def _conversation(pairs: int = 12) -> list:
	out: list = [HumanMessage(content="do the thing")]
	for i in range(pairs):
		cid = f"call_{i}"
		out.append(AIMessage(content=f"step {i}", tool_calls=[{"id": cid, "name": "web_fetch", "args": {}}]))
		out.append(ToolMessage(content=f"RESULT {i}\n{BIG}", tool_call_id=cid))
	return out


class _SummaryLLM:
	model_name = "gpt-5"

	def __init__(self):
		self.calls = 0

	async def ainvoke(self, messages, tools=None, **kwargs):
		self.calls += 1
		resp = MagicMock()
		resp.content = "## Summary\nThe agent fetched pages."
		resp.usage_metadata = None
		return resp


@pytest.fixture(autouse=True)
def _env(monkeypatch):
	monkeypatch.setenv("COMPACTION_CHECKPOINT", "false")
	monkeypatch.setenv("TASK_MAX_MESSAGES", "500")


def _mm(llm) -> MessageManager:
	# A small window keeps the protected tail short, so there is a span to summarise.
	return MessageManager(llm=llm, task="Test task", action_descriptions="Test actions",
	                      system_prompt_class=SystemPrompt, max_input_tokens=20_000,
	                      image_tokens=800, session_id="s-floor")


def test_ageing_is_enough_only_well_under_the_band():
	from agents.task.constants import COMPACTION_LLM_PCT, COMPACTION_WARN_PCT
	assert AGEING_ENOUGH_PCT <= COMPACTION_WARN_PCT < COMPACTION_LLM_PCT


@pytest.mark.asyncio
async def test_ageing_that_lands_just_under_the_band_still_runs_the_summariser(tmp_path, monkeypatch):
	llm = _SummaryLLM()
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
	for msg in _conversation():
		mm._add_message_with_tokens(msg, _internal=True)
	# Prod 16:43:37: ageing took the gauge from 85.5 % to 84.7 % only.
	monkeypatch.setattr(MessageManager, "_context_usage_percent_or_none", lambda self: 84.7)
	assert await mm.llm_compact_history()
	assert llm.calls == 1, "a 1 % ageing cut counted as a finished compaction"


@pytest.mark.asyncio
async def test_ageing_that_clears_the_band_by_a_margin_spares_the_summariser(tmp_path, monkeypatch):
	llm = _SummaryLLM()
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
	for msg in _conversation():
		mm._add_message_with_tokens(msg, _internal=True)
	monkeypatch.setattr(MessageManager, "_context_usage_percent_or_none",
	                    lambda self: AGEING_ENOUGH_PCT - 20)
	assert await mm.llm_compact_history()
	assert llm.calls == 0
