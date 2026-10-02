"""060 WS-2 ratchet — ONE foundation order, derived and tested (2026-09-23).

The foundation order used to exist three times, hand-maintained
(``retrieval.get_messages``, ``retrieval.get_messages_for_llm``,
``foundation_replay.FOUNDATION_SLOTS``), each commented as matching the others
and none tested. This file pins:

1. ONE order: both assemblies and the replay slots equal ``FOUNDATION_LAYERS``.
2. Every ``MessageOrigin`` maps to exactly one tier.
3. No VOLATILE block sits in the cached prefix (the default H-MEM placement is
   the tail).
4. ``retrieval.py`` names no foundation attribute itself — a second hand-kept
   copy of the order fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agents.task.agent.messages import foundation_layers as FL
from agents.task.agent.messages.foundation_replay import FOUNDATION_SLOTS
from modules.llm.messages import HumanMessage, MessageOrigin, SystemMessage

ROOT = Path(__file__).resolve().parents[1]


def _all_origins():
	return {
		v for k, v in vars(MessageOrigin).items()
		if not k.startswith("_") and isinstance(v, str)
	}


def test_every_origin_maps_to_exactly_one_tier():
	origins = _all_origins()
	assert set(FL.ORIGIN_TIERS) == origins, (
		"every MessageOrigin needs ONE tier row in foundation_layers.ORIGIN_TIERS; "
		f"missing={sorted(origins - set(FL.ORIGIN_TIERS))} "
		f"stale={sorted(set(FL.ORIGIN_TIERS) - origins)}")
	for origin, tier in FL.ORIGIN_TIERS.items():
		assert tier in FL.TIERS, (origin, tier)


def test_layer_origins_agree_with_origin_tiers():
	for layer in FL.FOUNDATION_LAYERS:
		if layer.origin is not None:
			assert FL.ORIGIN_TIERS[layer.origin] == layer.tier, layer


def test_no_volatile_block_in_cached_prefix():
	for layer in FL.FOUNDATION_LAYERS:
		assert layer.tier != FL.VOLATILE, f"VOLATILE layer {layer.key} in the cached prefix"
		assert layer.cached, layer
	assert FL.HMEM_LAYER.tier == FL.VOLATILE and not FL.HMEM_LAYER.cached


def test_default_hmem_rides_the_tail(monkeypatch):
	monkeypatch.delenv("HMEM_TAIL_PLACEMENT", raising=False)
	assert FL.hmem_placement() == "tail"


def test_system_prompt_is_first_and_keys_unique():
	assert FL.FOUNDATION_LAYERS[0].attr == "_system_message"
	keys = [layer.key for layer in FL.FOUNDATION_LAYERS]
	attrs = [layer.attr for layer in FL.FOUNDATION_LAYERS]
	assert len(set(keys)) == len(keys)
	assert len(set(attrs)) == len(attrs)


def test_replay_slots_are_derived_from_the_table():
	assert [s[0] for s in FOUNDATION_SLOTS] == [layer.key for layer in FL.FOUNDATION_LAYERS]
	assert [s[1] for s in FOUNDATION_SLOTS] == [layer.attr for layer in FL.FOUNDATION_LAYERS]


def test_retrieval_names_no_foundation_attribute():
	"""A second hand-kept copy of the order in retrieval.py fails here."""
	src = (ROOT / "agents/task/agent/messages/retrieval.py").read_text()
	offenders = []
	for layer in FL.FOUNDATION_LAYERS[1:]:
		if layer.attr == "_initial_task_message":
			continue  # the debug log names the task row; it never orders it
		if re.search(rf"\b{re.escape(layer.attr)}\b", src):
			offenders.append(layer.attr)
	assert not offenders, (
		f"retrieval.py names foundation attributes {offenders} — derive the order "
		"from foundation_layers.FOUNDATION_LAYERS instead")


def _manager_with_every_block():
	from agents.task.agent.message_manager.service import MessageManager
	from agents.task.agent.prompts import SystemPrompt

	class _LLM:
		model_name = "gpt-5"

	mm = MessageManager(
		llm=_LLM(), task="t", action_descriptions="a",
		system_prompt_class=SystemPrompt, max_input_tokens=128000,
		session_id="ratchet-foundation", use_native_tools=True)
	mm.set_runtime_identity("m", "p")
	mm.set_environment_message("env")
	mm.set_self_context_message("soul")
	mm.set_project_context_message("proj")
	mm.set_skill_message("skills")
	mm.set_worker_catalog_message("workers")
	mm.set_tool_catalog_message("catalog")
	return mm


def test_both_assemblies_share_the_table_order(monkeypatch):
	monkeypatch.setenv("HMEM_TAIL_PLACEMENT", "true")
	mm = _manager_with_every_block()
	expected = FL.foundation_messages(mm)
	# the two self-context layers are alternatives (060 WS-1): one is empty.
	assert len(expected) == len(FL.FOUNDATION_LAYERS) - 1
	assert isinstance(expected[0], SystemMessage)
	n = len(expected)
	assert mm.get_messages()[:n] == expected
	assert mm.get_messages_for_llm(consume_ephemeral=False)[:n] == expected


def test_one_self_context_layer_per_group():
	groups = {}
	for layer in FL.FOUNDATION_LAYERS:
		if layer.group:
			groups.setdefault(layer.group, []).append(layer)
	assert {g: len(v) for g, v in groups.items()} == {"self_context": 2}
	# alternatives sit next to each other so either render lands in one place.
	idx = [FL.FOUNDATION_LAYERS.index(layer) for layer in groups["self_context"]]
	assert idx[1] == idx[0] + 1


def test_layer_may_hold_several_messages():
	class _M:
		_system_message = SystemMessage(content="s")
		_self_context_message = (HumanMessage(content="a"), None, HumanMessage(content="b"))

	out = FL.foundation_messages(_M())
	assert [m.content for m in out] == ["s", "a", "b"]
