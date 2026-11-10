"""F21 — two consecutive in-session assemblies must share a prompt PREFIX.

Every provider cache is a prefix cache. Nothing in the tree asserted that
``get_messages_for_llm()`` at step N+1 extends the step-N assembly rather than
rewriting it, so four routine rewrites (image anchor, dedup renumber, deque
left-eviction, deepseek merge) went unnoticed for months.

The contract this file pins, for a step that runs a tool:

    assembly(N)   = foundation + pairs[1..N-1] + state_message(N)
    assembly(N+1) = foundation + pairs[1..N]   + state_message(N+1)

so the ONLY message of assembly(N) that assembly(N+1) may not repeat is the
trailing state message: it names the current step and the current clock, so its
bytes are new every step by construction and no cache can ever hold them. That
is the ``== len(previous) - 1`` below, and it is the ceiling — not a defect.

F16(ii) (2026-09-23) did not move that ceiling and was never able to: it took the
state message OUT of ``history`` (it rides the one-shot ephemeral rail now), so
the splice that rebuilt the deque every step is gone, the F6 provider-usage
anchor survives a step instead of being invalidated by that splice, the state
message no longer consumes a deque slot it is about to give back, and the
screenshot no longer passes through ``message_history.json``. The new invariant
is therefore about HISTORY, not about the assembly length, and it is asserted
below: no state message is ever in ``history``.

The xfail-marked tests below are the KNOWN breakers. Each one is flipped to a
plain pass by the item that fixes it; a breaker that starts passing early fails
loudly (``strict=True``) rather than rotting.
"""

from __future__ import annotations

import pytest

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.agent.views import AgentStepInfo
from modules.llm.messages import HumanMessage
from agents.task.loop_types.browser_views import BrowserState
from agents.task.loop_types.dom_views import DOMElementNode

from tests.support.prefix_cache import (
	PrefixCacheProbe,
	common_prefix_len,
	serialize_messages,
	wire_adapter,
	wire_prefix_len,
	wire_rows,
)


class _FakeChatModel:
	"""Minimal LLM stand-in — MessageManager only reads ``model_name`` off it."""

	def __init__(self, model_name: str = "gpt-5"):
		self.model_name = model_name


def _minimal_state() -> BrowserState:
	"""A BrowserState with no meaningful browser content (non-browser task)."""
	return BrowserState(
		url="",
		title="",
		element_tree=DOMElementNode(
			tag_name="body",
			attributes={},
			children=[],
			is_visible=True,
			parent=None,
			xpath="//body",
		),
		selector_map={},
		tabs=[],
	)


def _mm(model_name: str = "gpt-5", **kwargs) -> MessageManager:
	return MessageManager(
		llm=_FakeChatModel(model_name),
		task="Test task",
		action_descriptions="Test actions",
		system_prompt_class=SystemPrompt,
		max_input_tokens=kwargs.pop("max_input_tokens", 40000),
		image_tokens=800,
		session_id=kwargs.pop("session_id", "s-prefix"),
		**kwargs,
	)


def _run_step(mm: MessageManager, step: int, *, result: str) -> list:
	"""One agent step, in the order ``core/step.py`` runs it.

	add_state_message -> assemble -> add AI+tool pair -> remove_last_state_message
	(step.py:603-610 / :656). The last call is a no-op under F16(ii) and is kept
	here because the five call sites in ``core/step.py`` are kept.
	"""
	mm.add_state_message(
		state=_minimal_state(),
		step_info=AgentStepInfo(step_number=step, max_steps=20),
		use_vision=False,
		include_browser_state=False,
	)
	assembled = mm.get_messages_for_llm(consume_ephemeral=False)
	call_id = f"call_{step}"
	mm.add_tool_call_pair_atomic(
		ai_content=f"step {step}",
		tool_calls=[{"id": call_id, "name": "noop", "args": {"i": step}}],
		tool_responses=[(call_id, result)],
	)
	mm.remove_last_state_message()
	return assembled


# ---------------------------------------------------------------------------
# The baseline contract — this one passes today and must never regress
# ---------------------------------------------------------------------------


def test_six_steps_share_the_whole_previous_prefix_but_the_state_message():
	mm = _mm()
	probe = PrefixCacheProbe()

	for step in range(6):
		probe.record(_run_step(mm, step, result=f"tool output for step {step}"))

	for idx in range(1, len(probe.snapshots)):
		previous = probe.snapshots[idx - 1]
		current = probe.snapshots[idx]
		shared = common_prefix_len(previous, current)
		assert shared == len(previous) - 1, (
			f"assembly {idx} broke the prefix: kept {shared} of {len(previous) - 1} "
			f"reusable messages"
		)
		# The one message that legitimately does not repeat is the state message.
		assert "[Current state" in previous[-1] or "state" in previous[-1].lower()

	# F16(ii): and it was never in the durable history to begin with.
	assert not any((getattr(m.message, "metadata", None) or {}).get("state_message")
	               for m in mm.history.messages)


def test_probe_hit_ratio_is_near_one_across_a_run():
	mm = _mm()
	probe = PrefixCacheProbe()
	for step in range(6):
		probe.record(_run_step(mm, step, result=f"tool output for step {step}"))
	# Every assembly but the trailing state message is reusable.
	assert probe.hit_ratio > 0.9, probe.describe_last()


# ---------------------------------------------------------------------------
# F16(i) — dedup_tool_results embeds a POSITIONAL index in its back-reference
# ---------------------------------------------------------------------------


def _duplicate_payload() -> str:
	# Must clear DEDUP_MIN_CHARS (160) for the dedup pass to fire at all.
	return "IDENTICAL TOOL OUTPUT. " * 12


def test_dedup_backreference_bytes_survive_a_shift():
	"""The back-reference must name the DIGEST, never a list position."""
	mm = _mm()
	payload = _duplicate_payload()
	for step in range(2):
		_run_step(mm, step, result=payload)

	before = serialize_messages(mm.get_messages_for_llm(consume_ephemeral=False))
	# Anything landing ahead of the first occurrence renumbers the pointer.
	mm.add_message(HumanMessage(content="a note that lands ahead of the pair"), position=0)
	after = serialize_messages(mm.get_messages_for_llm(consume_ephemeral=False))

	ref_before = [s for s in before if "duplicate of an earlier tool result" in s]
	ref_after = [s for s in after if "duplicate of an earlier tool result" in s]
	assert ref_before and ref_after, "the dedup pass did not fire"
	assert ref_before == ref_after, (
		"the dedup back-reference changed bytes because a message moved:\n"
		f"  was: {ref_before}\n  now: {ref_after}"
	)


# ---------------------------------------------------------------------------
# F10 — deque left-eviction shifts the conversation by one on EVERY append
# ---------------------------------------------------------------------------


def test_prefix_survives_deque_saturation(monkeypatch):
	monkeypatch.setenv("TASK_MAX_MESSAGES", "12")
	mm = _mm(session_id="s-prefix-evict")
	probe = PrefixCacheProbe()

	for step in range(14):
		probe.record(_run_step(mm, step, result=f"tool output for step {step}"))

	warm = 0
	for idx in range(1, len(probe.snapshots)):
		previous = probe.snapshots[idx - 1]
		current = probe.snapshots[idx]
		if common_prefix_len(previous, current) >= len(previous) - 1:
			warm += 1
	# Saturation begins around step 5; a batched cut leaves most steps warm.
	assert warm >= 8, f"only {warm} of {len(probe.snapshots) - 1} assemblies were warm"


# ---------------------------------------------------------------------------
# F7 — strip_historical_media anchors on the NEWEST image
# ---------------------------------------------------------------------------


def _image_turn(step: int) -> HumanMessage:
	return HumanMessage(content=[
		{"type": "image_url",
		 "image_url": {"url": "data:image/png;base64," + ("A" * 64) + str(step)}},
		{"type": "text", "text": f"screenshot for step {step}"},
	])


def test_prefix_survives_successive_screenshots():
	mm = _mm()
	probe = PrefixCacheProbe()

	for step in range(6):
		mm._add_message_with_tokens(_image_turn(step))
		probe.record(mm.get_messages_for_llm(consume_ephemeral=False))

	for idx in range(1, len(probe.snapshots)):
		previous = probe.snapshots[idx - 1]
		current = probe.snapshots[idx]
		shared = common_prefix_len(previous, current)
		assert shared == len(previous), (
			f"assembly {idx} rewrote an earlier image turn: kept {shared} of "
			f"{len(previous)}"
		)


# ---------------------------------------------------------------------------
# F9 — the <tool-addition> announcement is an APPEND, never a rewrite
# ---------------------------------------------------------------------------


def test_tool_addition_announcement_is_appended_to_the_prefix():
	"""Surfacing a late tool must extend the assembly, not move anything in it.

	The whole point of shape (b) is that a late tool costs the cached prefix
	nothing: the schema rides ``tools[]`` marked ``defer_loading`` and the
	announcement is one NEW message at the tail. So assembly(N+1) still shares
	everything of assembly(N) but the trailing state message.

	⚠️ The provider-side half of that claim — that Anthropic really does leave
	the cached prefix intact — cannot be asserted offline.
	"""
	from modules.llm.messages import MessageOrigin, make_control_message

	mm = _mm()
	before = _run_step(mm, 0, result="tool output for step 0")
	mm.push_control_message(make_control_message(
		"late_echo\nlate_ping", MessageOrigin.TOOL_ADDITION))
	after = _run_step(mm, 1, result="tool output for step 1")

	shared = common_prefix_len(serialize_messages(before), serialize_messages(after))
	assert shared == len(before) - 1, (
		f"the announcement broke the prefix: kept {shared} of {len(before) - 1}")
	texts = [m.content if isinstance(m.content, str) else str(m.content)
	         for m in after]
	assert any("<tool-addition>\nlate_echo\nlate_ping\n</tool-addition>" in t
	           for t in texts)


def test_two_runs_of_one_job_share_the_prefix_through_the_catalogs():
	"""081 WS-3.3: each cron/goal run is a NEW session, so the cross-session
	prefix is what a provider cache can reuse. Two runs that differ only in the
	workspace path and the per-run task text share every foundation row up to
	and including the tool catalog."""
	def run(n: int):
		mm = MessageManager(
			llm=_FakeChatModel(), task=f"Daily job. <previous-runs>run {n}</previous-runs>",
			action_descriptions="a", system_prompt_class=SystemPrompt,
			max_input_tokens=128000, session_id=f"cron-run-{n}", use_native_tools=True)
		mm.set_runtime_identity("m", "p")
		mm.set_environment_message("<environment>the same install</environment>")
		mm.set_self_context_message("soul")
		mm.set_skill_message("skills")
		mm.set_tool_catalog_message("catalog")
		mm.set_workspace_message(f"Workspace (absolute): /data/sessions/u/cron-run-{n}/workspace")
		return serialize_messages(mm.get_messages_for_llm(consume_ephemeral=False))

	a, b = run(1), run(2)
	shared = common_prefix_len(a, b)
	assert shared == len(a) - 2, (shared, len(a))  # all but workspace + task
	assert "catalog" in "".join(a[:shared])


# ---------------------------------------------------------------------------
# 106 C13: H-MEM, a late tool and a saved reasoning, on the WIRE
# ---------------------------------------------------------------------------


class _ChangingMemory:
	"""H-MEM text that changes every step, as the real one does."""

	def __init__(self):
		self.calls = 0

	def get_context_injection(self, session_id):
		self.calls += 1
		return f"memory read {self.calls}"


def _wire_step(mm, adapter, step, tools):
	"""One step's request as it reaches the provider."""
	import asyncio
	mm.add_state_message(state=_minimal_state(),
	                     step_info=AgentStepInfo(step_number=step, max_steps=20),
	                     use_vision=False, include_browser_state=False)
	asyncio.run(adapter.ainvoke(mm.get_messages_for_llm(), tools=tools))
	call_id = f"call_{step}"
	mm.add_tool_call_pair_atomic(
		ai_content=f"step {step}",
		tool_calls=[{"id": call_id, "name": "noop", "args": {"i": step}}],
		tool_responses=[(call_id, f"tool output for step {step}")],
	)
	mm.commit_ephemeral_consumption()
	return adapter.wire_calls()[-1]


_DONE = {"type": "function", "function": {"name": "done", "parameters": {}}}
_LATE = {"type": "function", "function": {"name": "late_echo", "parameters": {}}}


def test_h_mem_stays_outside_the_wire_prefix():
	adapter = wire_adapter()
	mm = MessageManager(llm=adapter, task="Test task", action_descriptions="a",
	                    system_prompt_class=SystemPrompt, max_input_tokens=40000,
	                    session_id="s-hmem", task_context_manager=_ChangingMemory())
	first = _wire_step(mm, adapter, 0, [_DONE])
	second = _wire_step(mm, adapter, 1, [_DONE])
	assert "memory read" in str(first["messages"][-2:]), "the rig did not carry H-MEM"
	# Only the tail of the first request (H-MEM + the state message) may not repeat.
	assert wire_prefix_len(first, second) == len(wire_rows(first)) - 2


def test_a_late_tool_changes_only_the_tools_row():
	"""D1 keeps ``grow``: a late tool moves ``tools[]``. No message row moves with it."""
	adapter = wire_adapter()
	mm = MessageManager(llm=adapter, task="Test task", action_descriptions="a",
	                    system_prompt_class=SystemPrompt, max_input_tokens=40000,
	                    session_id="s-late")
	first = _wire_step(mm, adapter, 0, [_DONE])
	second = _wire_step(mm, adapter, 1, [_DONE, _LATE])
	assert wire_rows(first)[0] != wire_rows(second)[0]
	assert common_prefix_len(wire_rows(first)[1:], wire_rows(second)[1:]) == len(wire_rows(first)) - 2


def test_tools_order_and_bytes_hold_on_the_wire_from_step_to_step():
	adapter = wire_adapter()
	mm = MessageManager(llm=adapter, task="Test task", action_descriptions="a",
	                    system_prompt_class=SystemPrompt, max_input_tokens=40000,
	                    session_id="s-tools")
	rows = [wire_rows(_wire_step(mm, adapter, i, [_DONE, _LATE]))[0] for i in range(3)]
	assert rows[0] == rows[1] == rows[2]
