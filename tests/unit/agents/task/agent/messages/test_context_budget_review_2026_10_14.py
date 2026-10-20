"""2026-10-14 model-utilization review, group 4 — context and compaction.

E-1  LLM compaction keeps the owner's CURRENT request verbatim (re-inserted
     after the summary) and the summary frame says the latest owner message wins.
A-5  The warm-prefix summariser flattens a sequence slot (``_self_context_blocks``
     is a tuple) instead of sending the tuple as one "message".
B-5  An interactive session gauges its HISTORY against CHAT_HISTORY_BUDGET_TOKENS;
     the protected tail is sized against that budget, and the anti-thrash guard
     shrinks the tail instead of skipping while the tail is above the floor.
B-10 The TASK_MAX_INPUT_TOKENS / explicit override path reserves the CAPPED
     completion allowance, not a flat 4,096.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.messages import compactor as C
from agents.task.agent.messages import token_counter as TC
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import AIMessage, HumanMessage, MessageOrigin, make_control_message


class _RecordingLLM:
	def __init__(self, model_name: str = "gpt-5"):
		self.model_name = model_name
		self.requests: list = []

	async def ainvoke(self, messages, tools=None, **kwargs):
		self.requests.append(list(messages))
		return AIMessage(content="## Active Task\nsummarised")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
	monkeypatch.setenv("TASK_MAX_MESSAGES", "500")
	monkeypatch.setenv("COMPACTION_CHECKPOINT", "false")
	monkeypatch.delenv("TASK_MAX_INPUT_TOKENS", raising=False)
	monkeypatch.delenv("AUTONOMOUS_CONTEXT_BUDGET_TOKENS", raising=False)


def _mm(llm=None, session_id="s-g4", max_input_tokens=4000) -> MessageManager:
	if llm is None:
		llm = MagicMock()
		llm.model_name = "gpt-4o"
	return MessageManager(
		llm=llm, task="first message", action_descriptions="acts",
		system_prompt_class=SystemPrompt, max_input_tokens=max_input_tokens,
		session_id=session_id,
	)


def _fill(mm, turns=60, owner_ask_at=None, ask="please bridge 0.5 ETH to Base now"):
	for i in range(turns):
		if i == owner_ask_at:
			mm._add_message_with_tokens(HumanMessage(content=ask), _internal=True)
			continue
		body = f"turn {i}: " + ("conversation body " * 12)
		msg = (make_control_message(body, MessageOrigin.SELF_WAKE) if i % 2 == 0
		       else AIMessage(content=body))
		mm._add_message_with_tokens(msg, _internal=True)


# ----------------------------------------------------------------------- E-1

def test_llm_compaction_reinserts_the_latest_owner_turn_verbatim(tmp_path):
	mm = _mm(_RecordingLLM())
	mm._compaction_checkpoint_dir = str(tmp_path)
	_fill(mm, turns=80, owner_ask_at=3)
	assert asyncio.run(mm.llm_compact_history())

	kept = [m.message for m in mm.history.messages]
	assert C._COMPACTED_MARKER in str(kept[0].content)
	assert "the latest owner message wins" in str(kept[0].content)
	asks = [m for m in kept if str(m.content) == "please bridge 0.5 ETH to Base now"]
	assert len(asks) == 1, "the owner's current request must survive verbatim"
	assert getattr(asks[0], "origin", MessageOrigin.USER) == MessageOrigin.USER
	assert kept.index(asks[0]) == 1, "it rides right after the summary, before the tail"


def test_no_reinsert_when_the_tail_already_holds_an_owner_turn(tmp_path):
	mm = _mm(_RecordingLLM())
	mm._compaction_checkpoint_dir = str(tmp_path)
	_fill(mm, turns=80, owner_ask_at=3)
	mm._add_message_with_tokens(HumanMessage(content="newer ask"), _internal=True)
	assert asyncio.run(mm.llm_compact_history())
	contents = [str(m.message.content) for m in mm.history.messages]
	assert "please bridge 0.5 ETH to Base now" not in contents
	assert contents.count("newer ask") == 1


def test_latest_current_request_skips_peer_batches_for_the_owner():
	from agents.task.agent.messages.guidance import PEER_LINE_PREFIX
	owner = HumanMessage(content="owner ask")
	peer = HumanMessage(content=f"{PEER_LINE_PREFIX} agent says hi")
	assert C.latest_current_request([owner, peer], []) is owner
	assert C.latest_current_request([owner, peer], [], owner_authority=False) is peer
	assert C.latest_current_request([owner], [HumanMessage(content="x")]) is None


# ----------------------------------------------------------------------- A-5

@pytest.mark.asyncio
async def test_warm_prefix_flattens_a_tuple_slot(tmp_path):
	llm = _RecordingLLM()
	mm = _mm(llm)
	mm._compaction_checkpoint_dir = str(tmp_path)
	mm.set_self_context_blocks((("soul", "SOUL BLOCK"), ("owner_rules", "OWNER RULES")))
	assert isinstance(mm._self_context_blocks, tuple)
	_fill(mm, turns=80)
	assert await mm.llm_compact_history()

	request = llm.requests[-1]
	assert all(hasattr(m, "content") for m in request), "a tuple rode as one message"
	texts = [str(m.content) for m in request]
	assert any("SOUL BLOCK" in t for t in texts)
	assert any("OWNER RULES" in t for t in texts)
	# warm shape: more than the lone cold instruction
	assert len(request) > 2
	assert not getattr(llm, C._WARM_UNAVAILABLE_ATTR, False)


# ----------------------------------------------------------------------- B-5

def test_an_owner_chat_gauges_history_against_the_chat_budget():
	mm = _mm(session_id="s-g4-chat", max_input_tokens=200_000)
	assert mm.max_input_tokens == 200_000, "the total safety cap is unchanged"
	assert mm._history_budget_tokens == TC.CHAT_HISTORY_BUDGET_TOKENS == 80_000
	# 93k fixed prefix + 69k history: the old gauge read 81 %; history now decides.
	mm.set_tool_schema_tokens(55_000)
	mm._self_context_tokens = 38_000
	mm.history.total_tokens = 69_000
	assert mm.get_context_usage_percent() >= 85
	mm.history.total_tokens = 40_000
	assert mm.get_context_usage_percent() < 70


def test_a_small_window_keeps_the_whole_prompt_gauge():
	mm = _mm(session_id="s-g4-small", max_input_tokens=60_000)
	assert mm._history_budget_tokens == 0


def test_tail_is_sized_against_the_history_budget():
	mm = _mm(session_id="s-g4-tail", max_input_tokens=200_000)
	# 60 messages of ~1k tokens: 20 % of 200k would keep 40 of them.
	msgs = [HumanMessage(content="y" * 4000) for _ in range(60)]
	keep = mm._compaction_keep_recent(msgs)
	assert keep == int(TC.CHAT_HISTORY_BUDGET_TOKENS * C._TAIL_HISTORY_RATIO) // 1000


class _Harness(C.CompactorMixin):
	def __init__(self, llm, history_budget=0, max_input_tokens=200_000):
		from agents.task.agent.message_manager.views import MessageHistory
		self.logger = logging.getLogger("g4")
		self.history = MessageHistory()
		self.llm = llm
		self.max_input_tokens = max_input_tokens
		self._history_budget_tokens = history_budget

	def _add_message_with_tokens(self, message, _internal=False):
		from agents.task.agent.message_manager.views import ManagedMessage, MessageMetadata
		tokens = max(1, len(str(message.content)) // 4)
		self.history.messages.append(
			ManagedMessage(message=message, metadata=MessageMetadata(input_tokens=tokens)))
		self.history.total_tokens += tokens

	def get_context_usage_percent(self):
		return 90.0


def test_thrash_shrinks_the_tail_instead_of_skipping():
	llm = _RecordingLLM()
	h = _Harness(llm, history_budget=80_000)
	h._compaction_savings = [0.07, 0.06]
	for i in range(60):
		h._add_message_with_tokens(HumanMessage(content=("x" * 4000) + f"-{i}"))
	assert asyncio.run(h.llm_compact_history()) is True
	assert len(llm.requests) == 1
	# summary + the 10-message floor
	assert len(h.history.messages) <= 1 + C._MIN_KEEP_RECENT + 1
	assert h._compaction_savings[-1] >= 0.10


# ----------------------------------------------------------------------- B-10

def test_override_path_reserves_the_capped_completion_allowance(monkeypatch):
	class _Host(TC.TokenCounterMixin):
		def __init__(self):
			self.logger = logging.getLogger("g4.tokens")
			self._model_name = "x-ai/grok-4.3"

	from modules.llm.model_registry import get_model_config
	cfg = get_model_config("x-ai/grok-4.3")
	assert cfg and cfg.max_completion_tokens
	monkeypatch.setenv("TASK_MAX_INPUT_TOKENS", "200000")
	_mx, _safe, reserve = _Host()._calculate_token_limits(None, None)
	assert reserve == TC._capped_reserve(int(cfg.max_completion_tokens))
	assert reserve != 4096 or int(cfg.max_completion_tokens) == 4096
