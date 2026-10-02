"""060 WS-1 — the SELF_CONTEXT weld is split into declared blocks.

Before: awareness + SOUL + owner facts + contract + SELF doc were ONE message
(``"\\n\\n".join``). The owner's rules could not be capped, dated or reported
apart from an operator-frozen identity doc. Now each non-empty block is its own
SELF_CONTEXT foundation message; ``SELF_CONTEXT_COMBINED=true`` re-joins them
byte-identically.
"""

import json
from unittest.mock import MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.messages.foundation_layers import SELF_CONTEXT_BLOCKS
from agents.task.agent.messages.foundation_replay import capture_foundation, replay_foundation
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import MessageOrigin, make_control_message

BLOCKS = (
	("awareness", "You act on behalf of OWNER A."),
	("soul", "SOUL: I am Rob."),
	("owner_rules", "## Owner facts\n\nNo public bug-fix reports."),
	("contract", ""),
	("self_doc", "SELF: I learned X."),
)


def _mm():
	llm = MagicMock()
	llm.model_name = "gpt-4o"
	return MessageManager(
		llm=llm, task="Test task", action_descriptions="acts",
		system_prompt_class=SystemPrompt, max_input_tokens=8000,
		session_id="ws1-blocks")


def _self_ctx(msgs):
	return [m for m in msgs if getattr(m, "origin", None) == MessageOrigin.SELF_CONTEXT]


def test_block_names_are_declared_in_wire_order():
	assert [n for n, _t in SELF_CONTEXT_BLOCKS] == [n for n, _t in BLOCKS]


def test_unwelded_default_one_message_per_nonempty_block(monkeypatch):
	monkeypatch.delenv("SELF_CONTEXT_COMBINED", raising=False)
	mm = _mm()
	mm.set_self_context_blocks(BLOCKS)
	got = _self_ctx(mm.get_messages_for_llm(consume_ephemeral=False))
	assert len(got) == 4  # the empty contract block is omitted
	assert [m.content for m in got] == [
		make_control_message(t, MessageOrigin.SELF_CONTEXT).content
		for _n, t in BLOCKS if t]
	assert mm._self_context_message is None
	assert mm._self_context_blocks_tokens > 0
	assert mm.get_total_tokens() >= mm._self_context_blocks_tokens


def test_combined_flag_is_byte_identical_to_the_pre_060_join(monkeypatch):
	monkeypatch.setenv("SELF_CONTEXT_COMBINED", "true")
	mm = _mm()
	mm.set_self_context_blocks(BLOCKS)
	legacy = "\n\n".join(p for _n, p in BLOCKS if p)
	got = _self_ctx(mm.get_messages_for_llm(consume_ephemeral=False))
	assert len(got) == 1
	assert got[0].content == make_control_message(legacy, MessageOrigin.SELF_CONTEXT).content
	assert mm._self_context_blocks is None


def test_the_two_slots_never_coexist(monkeypatch):
	monkeypatch.delenv("SELF_CONTEXT_COMBINED", raising=False)
	mm = _mm()
	mm.set_self_context_blocks(BLOCKS)
	mm.set_self_context_message("joined")
	assert mm._self_context_blocks is None
	mm.set_self_context_blocks(BLOCKS)
	assert mm._self_context_message is None


def test_all_empty_is_inert(monkeypatch):
	monkeypatch.delenv("SELF_CONTEXT_COMBINED", raising=False)
	mm = _mm()
	mm.set_self_context_blocks((("soul", ""), ("owner_rules", "  ")))
	assert not _self_ctx(mm.get_messages_for_llm(consume_ephemeral=False))
	assert mm._self_context_blocks_tokens == 0


def test_replay_round_trips_the_blocks(monkeypatch):
	monkeypatch.delenv("SELF_CONTEXT_COMBINED", raising=False)
	a = _mm()
	a.set_self_context_blocks(BLOCKS)
	blob = json.loads(json.dumps(capture_foundation(a)))
	assert isinstance(blob["self_context_blocks"], list)
	b = _mm()
	b.set_self_context_blocks((("soul", "a different render"),))
	assert replay_foundation(b, blob)
	assert [m.content for m in b._self_context_blocks] == [m.content for m in a._self_context_blocks]
	assert b._self_context_message is None


def test_a_pre_060_blob_replays_the_weld_and_clears_live_blocks(monkeypatch):
	"""An F13 blob written before 060 has no blocks key: it is still whole, and
	its combined message must not stack on the freshly rendered blocks."""
	monkeypatch.delenv("SELF_CONTEXT_COMBINED", raising=False)
	old = _mm()
	old.set_self_context_message("the old weld")
	blob = json.loads(json.dumps(capture_foundation(old)))
	blob.pop("self_context_blocks", None)
	live = _mm()
	live.set_self_context_blocks(BLOCKS)
	assert replay_foundation(live, blob)
	got = _self_ctx(live.get_messages_for_llm(consume_ephemeral=False))
	assert [m.content for m in got] == [old._self_context_message.content]
	assert live._self_context_blocks is None


def test_worker_catalog_replay_respects_disabled_feature(monkeypatch):
    from agents.task.agent.messages.foundation_replay import capture_foundation, replay_foundation
    from agents.task.agent.profile_store import render_worker_catalog
    monkeypatch.setenv("WORKERS_ENABLED", "true")
    saved = _mm()
    saved.set_worker_catalog_message("- researcher: previously approved")
    blob = capture_foundation(saved)
    monkeypatch.setenv("WORKERS_ENABLED", "false")
    resumed = _mm()
    resumed.set_worker_catalog_message(render_worker_catalog("owner"))
    assert replay_foundation(resumed, blob)
    assert resumed._worker_catalog_message is None
    assert resumed._worker_catalog_tokens == 0
    assert not any("<worker-catalog>" in str(m.content)
                   for m in resumed.get_messages_for_llm(consume_ephemeral=False))
