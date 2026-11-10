"""103: a stored compaction summary is complete, bounded, and names any gap."""
import logging

import pytest

from agents.task.agent.message_manager.views import (
	ManagedMessage, MessageHistory, MessageMetadata)
from agents.task.agent.messages import summary_contract as SC
from agents.task.agent.messages.compactor import CompactorMixin
from modules.llm.call_result import CallFacts
from modules.llm.messages import AIMessage, HumanMessage


def _whole(extra: str = "") -> str:
	body = "\n".join(f"## {name}\n- {name} item{extra}" for name in SC.REQUIRED_SECTIONS)
	return f"{body}\n{SC.END_LINE}"


def test_only_an_answer_that_reached_its_end_line_is_complete():
	assert SC.is_complete(_whole())
	assert SC.is_complete(_whole() + "\n\n")
	assert not SC.is_complete(_whole()[:-5])
	assert not SC.is_complete("")
	assert not SC.is_complete(f"{SC.END_LINE} and more")


def test_a_call_the_provider_says_it_cut_is_not_complete_even_with_its_end_line():
	assert not SC.is_complete(_whole(), CallFacts(finish_reason="length"))
	assert SC.is_complete(_whole(), CallFacts(finish_reason="stop"))
	assert SC.is_complete(_whole(), None)


@pytest.mark.parametrize("name", SC.REQUIRED_SECTIONS)
def test_a_cut_before_any_section_is_seen(name):
	text = _whole()
	assert name in SC.missing_sections(text[:text.index(f"## {name}")])


def test_strip_end_line_leaves_the_sections():
	assert SC.END_LINE not in SC.strip_end_line(_whole())
	assert SC.missing_sections(SC.strip_end_line(_whole())) == []


def test_bound_summary_cannot_grow_past_the_section_limit_and_keeps_the_newest():
	lines = "\n".join(f"- action {i}" for i in range(2000))
	out = SC.bound_summary(f"## Completed Actions\n{lines}", section_limit=500)
	assert len(out) < 600
	assert "- action 0\n" in out
	assert "- action 1999" not in out
	assert SC.OLDER_CUT_LINE in out


def test_bound_summary_never_cuts_an_owner_instruction():
	rules = "\n".join(f"- rule {i}" for i in range(2000))
	heading = "Owner instructions still in force (verbatim, dated)"
	assert "- rule 1999" in SC.bound_summary(f"## {heading}\n{rules}", section_limit=100)


def test_bound_summary_merges_a_repeated_heading_newest_first():
	out = SC.bound_summary("## Remaining Work\nold\n\n## Remaining Work\nnew")
	assert out.count("## Remaining Work") == 1
	assert out.index("new") < out.index("old")


def test_salvage_keeps_whole_sections_drops_the_broken_line_and_names_the_gap():
	cut = "## Active Task\nthe task\n## Completed Actions\n- newest\n- older\n- brok"
	prior = "## Relevant Files & Data\n- a/file.md\n## Active Task\nstale task"
	out = SC.salvage(cut, prior)
	assert "the task" in out and "stale task" not in out
	assert "- newest" in out and "- brok" not in out
	assert "- a/file.md" in out
	assert "Older text, from the earlier summary: Relevant Files & Data" in out
	assert "Remaining Work" in out.split("Lost:")[1]
	assert SC.END_LINE not in out


def test_salvage_uses_the_deterministic_fallback_for_a_section_nothing_else_holds():
	out = SC.salvage("## Active Task\nx\ny", "", "## Pending User Asks\n- the ask")
	assert "- the ask" in out
	assert "Lost:" in out and "Pending User Asks" not in out.split("Lost:")[1]


def test_a_damaged_prior_summary_is_named_and_a_whole_one_is_not():
	assert SC.damaged_prior_note(SC.strip_end_line(_whole())) == ""
	assert SC.damaged_prior_note("no sections here") == ""
	note = SC.damaged_prior_note("## Active Task\nx\n## Completed Actions\ny")
	assert "Relevant Files & Data" in note and "Completed Actions" not in note


class _Harness(CompactorMixin):
	def __init__(self, llm):
		self.logger = logging.getLogger("test_summary_contract")
		self.history = MessageHistory()
		self.llm = llm
		self.max_input_tokens = 1000

	def _add_message_with_tokens(self, message, _internal=False):
		tokens = max(1, len(str(message.content)) // 4)
		self.history.messages.append(
			ManagedMessage(message=message, metadata=MessageMetadata(input_tokens=tokens)))
		self.history.total_tokens += tokens

	def get_context_usage_percent(self):
		return 90.0


class _CappedLLM:
	"""Answers from a script, like a model that stops at its output cap."""

	def __init__(self, *answers):
		self.answers = list(answers)
		self.prompts = []

	async def ainvoke(self, messages, tools=None, **kwargs):
		self.prompts.append(str(messages[-1].content))
		return AIMessage(content=self.answers.pop(0))


def _fill(h, n=40):
	for i in range(n):
		cls = HumanMessage if i % 2 == 0 else AIMessage
		h._add_message_with_tokens(cls(content=f"turn {i} " + "body " * 40))


def _summary(h) -> str:
	return str(h.history.messages[0].message.content)


@pytest.fixture(autouse=True)
def _no_checkpoint(monkeypatch):
	monkeypatch.setenv("COMPACTION_CHECKPOINT", "false")


@pytest.mark.asyncio
async def test_a_complete_answer_is_stored_once_without_the_end_line():
	llm = _CappedLLM(_whole())
	h = _Harness(llm)
	_fill(h)
	assert await h.llm_compact_history()
	assert len(llm.prompts) == 1
	assert "Remaining Work item" in _summary(h)
	assert SC.END_LINE not in _summary(h)


@pytest.mark.asyncio
async def test_a_cut_answer_retries_once_with_a_shorter_request():
	llm = _CappedLLM("## Active Task\ncut mid", _whole(" second"))
	h = _Harness(llm)
	_fill(h)
	assert await h.llm_compact_history()
	assert len(llm.prompts) == 2
	assert llm.prompts[1].startswith("Your previous answer was cut")
	assert "Remaining Work item second" in _summary(h)
	assert "cut mid" not in _summary(h)


@pytest.mark.asyncio
async def test_cut_twice_stores_a_salvage_that_names_the_gap():
	cut = "## Active Task\nthe task\n## Completed Actions\n- newest\n- brok"
	llm = _CappedLLM(cut, cut)
	h = _Harness(llm)
	_fill(h)
	assert await h.llm_compact_history()
	assert len(llm.prompts) == 2
	text = _summary(h)
	assert "- newest" in text and "- brok" not in text
	assert "## Compaction note" in text
	assert "Lost:" in text


@pytest.mark.asyncio
async def test_the_prompt_names_the_size_and_a_damaged_prior_summary():
	llm = _CappedLLM(_whole())
	h = _Harness(llm)
	prompt = h._build_compaction_prompt(
		[HumanMessage(content="x")], prior_summary="## Active Task\nx")
	assert f"never more than {SC.SUMMARY_MAX_WORDS} words" in prompt
	assert "drop nothing" not in prompt
	assert "LOST these sections" in prompt
	assert prompt.rstrip().endswith("STRUCTURED SUMMARY:")
	assert f"The LAST line of your answer must be exactly: {SC.END_LINE}" in prompt


def test_the_tail_counts_tool_call_arguments():
	h = _Harness(_CappedLLM())
	h._history_budget_tokens = 1000   # 400 tokens of tail
	msgs = [HumanMessage(content="x" * 40) for _ in range(30)]
	plain = h._compaction_keep_recent(msgs)
	heavy = [AIMessage(content="", tool_calls=[{"name": "write_file",
	                                            "args": {"content": "y" * 4000}}])
	         for _ in range(30)]
	assert h._compaction_keep_recent(heavy) == 10   # the floor: each call is over budget
	assert plain == 30
