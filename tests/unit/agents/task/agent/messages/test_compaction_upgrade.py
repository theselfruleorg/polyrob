"""Tests for the Reference-parity compaction upgrade (Tiers A/B/C).

See docs/REFERENCE_VS_ROB_CONTEXT_SYSTEM_2026-06.md §7 (test plan) and §9 (patches).

These tests are deliberately light: a small harness composes CompactorMixin against
a real MessageHistory and a stub LLM, so the compaction logic is exercised with no
network and no browser state.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from modules.llm.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from agents.task.agent.messages.compactor import CompactorMixin
from agents.task.agent.message_manager.views import (
    ManagedMessage,
    MessageHistory,
    MessageMetadata,
)


# --------------------------------------------------------------------------- #
# Test harness
# --------------------------------------------------------------------------- #
class _StubLLM:
    """Records calls; echoes a configurable reply (or raises)."""

    def __init__(self, reply: str = "STUB SUMMARY", fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls: list = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("aux llm down")
        return AIMessage(content=self.reply)


class _Harness(CompactorMixin):
    """Minimal MessageManager stand-in providing exactly what the mixin needs."""

    def __init__(self, llm=None, aux_llm=None, max_input_tokens: int = 1000):
        self.logger = logging.getLogger("test_compaction_upgrade")
        self.history = MessageHistory()
        self.llm = llm or _StubLLM()
        if aux_llm is not None:
            self.aux_llm = aux_llm
        self.max_input_tokens = max_input_tokens
        self._usage = 90.0

    def _add_message_with_tokens(self, message, _internal: bool = False):
        tokens = max(1, len(str(message.content)) // 4)
        self.history.messages.append(
            ManagedMessage(message=message, metadata=MessageMetadata(input_tokens=tokens))
        )
        self.history.total_tokens += tokens

    def get_context_usage_percent(self) -> float:
        return self._usage


def _fill(h: _Harness, n: int, size: int = 4000):
    """Add n large messages so the token-budget tail (C3) floors at min_keep=10."""
    for i in range(n):
        h._add_message_with_tokens(HumanMessage(content=("x" * size) + f"-m{i}"))


# --------------------------------------------------------------------------- #
# Tier A — compaction payload fixes
# --------------------------------------------------------------------------- #
def test_a1_oldest_middle_messages_reach_the_summarizer():
    """Defect A: the oldest messages must not be silently dropped (was [-50:])."""
    h = _Harness()
    msgs = [HumanMessage(content=f"MARK-{i}-zzz") for i in range(200)]
    prompt = h._build_compaction_prompt(msgs)
    assert "MARK-0-zzz" in prompt, "oldest message was dropped before summarization"
    assert "MARK-199-zzz" in prompt


def test_a2_tool_results_are_serialized_not_a_literal():
    """Defect B (part 1): tool output content must reach the summary input."""
    h = _Harness()
    payload = "TOOLDATA_NEEDLE_" + ("z" * 200)
    prompt = h._build_compaction_prompt([ToolMessage(content=payload, tool_call_id="c1")])
    assert "TOOLDATA_NEEDLE_" in prompt, "tool result reduced to a bare marker"


def test_a2_message_not_hard_truncated_at_500_chars():
    """Defect B (part 2): content past 500 chars must survive."""
    h = _Harness()
    content = ("a" * 600) + "NEEDLE_PAST_500"
    prompt = h._build_compaction_prompt([HumanMessage(content=content)])
    assert "NEEDLE_PAST_500" in prompt


def test_a3_structured_template_has_sections():
    h = _Harness()
    prompt = h._build_compaction_prompt([HumanMessage(content="hi")] * 6)
    for section in (
        "## Active Task",
        "## In Progress",
        "## Resolved Questions",
        "## Pending User Asks",
        "## Remaining Work",
    ):
        assert section in prompt, f"missing template section {section!r}"
    # I-10: the prompt must explicitly instruct
    # the summarizer to carry resolved decisions and open threads across compaction —
    # bare section headers alone let the agent re-ask settled questions or drop open
    # threads once the raw history is gone.
    assert "re-asked" in prompt and "re-litigated" in prompt, (
        "missing instruction that resolved questions/decisions must not be re-asked "
        "or re-litigated after compaction"
    )
    assert "survives compaction" in prompt, (
        "missing instruction that pending/open questions must survive compaction"
    )


def test_a4_prior_summary_is_fed_back_for_update():
    h = _Harness()
    prompt = h._build_compaction_prompt(
        [HumanMessage(content="hi")], prior_summary="PRIOR_SUMMARY_BODY_123"
    )
    assert "PRIOR_SUMMARY_BODY_123" in prompt
    assert "PRIOR SUMMARY" in prompt


def test_a4_existing_compacted_block_detected_and_not_re_summarized():
    """An existing [COMPACTED SESSION HISTORY] in the middle is fed back, not re-summarized."""
    h = _Harness(llm=_StubLLM(reply="NEW SUMMARY"))
    h._add_message_with_tokens(
        HumanMessage(content="[COMPACTED SESSION HISTORY]\n\nOLD_SUMMARY_TEXT\n\n[END]")
    )
    _fill(h, 19)
    asyncio.run(h.llm_compact_history())
    sent_prompt = h.llm.calls[0][0].content
    assert "OLD_SUMMARY_TEXT" in sent_prompt
    assert "PRIOR SUMMARY" in sent_prompt


def test_a5_routes_to_aux_llm_when_present():
    aux = _StubLLM(reply="AUX")
    main = _StubLLM(reply="MAIN")
    h = _Harness(llm=main, aux_llm=aux)
    _fill(h, 20)
    assert asyncio.run(h.llm_compact_history()) is True
    assert len(aux.calls) == 1
    assert len(main.calls) == 0


def test_a5_falls_back_to_main_llm_without_aux():
    main = _StubLLM(reply="MAIN")
    h = _Harness(llm=main)
    _fill(h, 20)
    assert asyncio.run(h.llm_compact_history()) is True
    assert len(main.calls) == 1


def test_a6_static_fallback_used_when_llm_fails():
    h = _Harness(llm=_StubLLM(fail=True))
    emergency_called = {"v": False}
    h.emergency_context_prune = lambda: emergency_called.__setitem__("v", True)
    for i in range(20):
        # large enough that the C3 token-budget tail floors at min_keep (so the
        # middle is non-empty and compaction actually runs). Paired AIMessage →
        # ToolMessage so the CX-H2 orphan-guard doesn't extend the tail past
        # the whole (otherwise all-orphan) history and skip compaction.
        body = ("padding " * 500) + f"ran tool /path/file{i}.py -> error: boom {i}"
        h._add_message_with_tokens(
            AIMessage(content="", tool_calls=[{"id": f"c{i}", "name": "run", "args": {}}])
        )
        h._add_message_with_tokens(ToolMessage(content=body, tool_call_id=f"c{i}"))
    assert asyncio.run(h.llm_compact_history()) is True
    assert emergency_called["v"] is False, "fell through to emergency prune instead of static fallback"
    joined = " ".join(str(m.message.content) for m in h.history.messages)
    assert "COMPACTED SESSION HISTORY" in joined


# --------------------------------------------------------------------------- #
# Tier B — cache breadth, dedup, media hygiene, anti-thrash
# --------------------------------------------------------------------------- #
def _has_cache_control(block_holder) -> bool:
    content = block_holder.get("content")
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("cache_control") for b in content)
    return False


def test_b1_marks_system_plus_last_three_messages():
    from modules.llm.anthropic_client import _apply_conversation_cache

    msgs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    out = _apply_conversation_cache(msgs, n=3)
    marked = [m for m in out if _has_cache_control(m)]
    assert len(marked) == 3
    # the three marked must be the last three
    assert all(_has_cache_control(m) for m in out[-3:])
    assert not any(_has_cache_control(m) for m in out[:-3])


def test_b1_disabled_by_env(monkeypatch):
    from modules.llm.anthropic_client import _apply_conversation_cache

    monkeypatch.setenv("ANTHROPIC_PROMPT_CACHE", "0")
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    out = _apply_conversation_cache(msgs, n=3)
    assert not any(_has_cache_control(m) for m in out)


def test_b2_identical_tool_results_deduped():
    from agents.task.agent.messages.filters import dedup_tool_results

    msgs = [
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "x", "args": {}}]),
        ToolMessage(content="IDENTICAL_OUTPUT", tool_call_id="c1"),
        AIMessage(content="", tool_calls=[{"id": "c2", "name": "x", "args": {}}]),
        ToolMessage(content="IDENTICAL_OUTPUT", tool_call_id="c2"),
    ]
    out = dedup_tool_results(msgs)
    tool_contents = [str(m.content) for m in out if isinstance(m, ToolMessage)]
    assert tool_contents[0] == "IDENTICAL_OUTPUT"
    assert tool_contents[1] != "IDENTICAL_OUTPUT"
    assert "duplicate" in tool_contents[1].lower() or "identical to" in tool_contents[1].lower()


def test_b3_keeps_latest_image_strips_older():
    from agents.task.agent.messages.filters import strip_historical_media

    def img():
        return {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJDRA=="}}

    txt = {"type": "text", "text": "hi"}
    msgs = [
        HumanMessage(content=[txt, img()]),  # older -> strip
        HumanMessage(content="middle"),
        HumanMessage(content=[txt, img()]),  # latest -> keep
    ]
    out = strip_historical_media(msgs)

    def has_b64(m):
        c = m.content
        if isinstance(c, list):
            return any(
                isinstance(b, dict)
                and b.get("type") == "image_url"
                and "base64" in str(b.get("image_url", {}).get("url", ""))
                for b in c
            )
        return False

    assert not has_b64(out[0]), "older image base64 should be stripped"
    assert has_b64(out[2]), "latest image base64 should be preserved"


def test_b4_thrash_detector_needs_two_low_savings():
    h = _Harness()
    h._compaction_savings = [0.05, 0.04]
    assert h._compaction_is_thrashing() is True
    h._compaction_savings = [0.05, 0.50]
    assert h._compaction_is_thrashing() is False
    h._compaction_savings = [0.04]
    assert h._compaction_is_thrashing() is False


def test_b4_skips_llm_call_when_thrashing():
    main = _StubLLM(reply="s")
    h = _Harness(llm=main)
    h._compaction_savings = [0.05, 0.04]
    _fill(h, 20)
    assert asyncio.run(h.llm_compact_history()) is False
    assert len(main.calls) == 0


def test_b4_records_savings_after_compaction():
    h = _Harness(llm=_StubLLM(reply="tiny"))
    _fill(h, 20)
    asyncio.run(h.llm_compact_history())
    assert len(h._compaction_savings) == 1
    assert 0.0 <= h._compaction_savings[0] <= 1.0


def test_b4_emergency_prune_resets_thrash_signal():
    """Recovery: a hard prune changes history, so the anti-thrash signal must clear
    (otherwise LLM compaction stays permanently disabled for the session)."""
    h = _Harness()
    h._compaction_savings = [0.05, 0.04]
    for i in range(20):
        h._add_message_with_tokens(HumanMessage(content=f"m{i}"))
    h.emergency_context_prune()
    assert h._compaction_savings == []
    assert h._compaction_is_thrashing() is False


def test_a1_summarizes_in_windows_when_middle_is_huge():
    """A small aux model must not be overflowed: a huge middle is summarized in
    iterative windows, and the oldest content still reaches the summarizer."""
    h = _Harness(llm=_StubLLM(reply="WIN"), max_input_tokens=1000)
    for i in range(250):
        h._add_message_with_tokens(HumanMessage(content=f"MARK{i}-" + ("q" * 3500)))
    assert asyncio.run(h.llm_compact_history()) is True
    assert len(h.llm.calls) > 1, "huge middle should be windowed into multiple calls"
    all_sent = " ".join(c[0].content for c in h.llm.calls)
    assert "MARK0-" in all_sent, "oldest message must still reach the summarizer (no drop)"


# --------------------------------------------------------------------------- #
# Tier C — references, checkpoint, token-budget tail
# --------------------------------------------------------------------------- #
def test_c1_expands_file_reference(tmp_path):
    from agents.task.agent.messages.context_references import preprocess_context_references

    f = tmp_path / "note.txt"
    f.write_text("HELLO_FROM_FILE_C1")
    out = preprocess_context_references(f"see @file:{f} please", context_length=100_000)
    assert "HELLO_FROM_FILE_C1" in out


def test_c1_hard_cap_blocks_oversized_injection(tmp_path):
    from agents.task.agent.messages.context_references import preprocess_context_references

    f = tmp_path / "big.txt"
    f.write_text("Z" * 10_000)
    # context_length tiny -> 50% hard cap is ~10 chars -> must NOT inline the 10k file
    out = preprocess_context_references(f"@file:{f}", context_length=20)
    assert "Z" * 10_000 not in out


def test_c2_writes_pre_compaction_checkpoint(tmp_path):
    h = _Harness(llm=_StubLLM(reply="s"))
    h._compaction_checkpoint_dir = str(tmp_path)
    _fill(h, 20)
    asyncio.run(h.llm_compact_history())
    files = list(tmp_path.glob("compaction_*.json"))
    assert len(files) == 1


def test_c3_keeps_more_than_floor_when_budget_generous():
    h = _Harness(max_input_tokens=10_000)
    msgs = [HumanMessage(content="x" * 40) for _ in range(60)]  # ~10 tok each
    assert h._compaction_keep_recent(msgs, min_keep=10) > 10


def test_c3_floors_at_min_keep_when_budget_tight():
    h = _Harness(max_input_tokens=100)
    msgs = [HumanMessage(content="x" * 4000) for _ in range(20)]  # ~1000 tok each
    assert h._compaction_keep_recent(msgs, min_keep=10) == 10


def test_real_message_manager_declares_compaction_slots():
    """The real MessageManager is __slots__-based; the new attrs must be slotted
    or construction.py / the mixin writes would raise AttributeError at runtime."""
    from agents.task.agent.message_manager.service import MessageManager

    for slot in ("aux_llm", "_compaction_savings", "_compaction_count", "_compaction_checkpoint_dir"):
        assert slot in MessageManager.__slots__
