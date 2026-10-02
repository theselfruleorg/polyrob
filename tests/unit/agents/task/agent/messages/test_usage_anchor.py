"""F6 — the provider's own prompt_tokens anchors the context gauge.

Before this, `prompt_tokens` and `cache_read_input_tokens` were extracted,
billed and then DISCARDED: every gauge, every compaction threshold and the
pre-call safety check ran on `len(text)/N`, with no tokenizer for Anthropic at
all. If that estimate is 15 % low, the 85 % compaction band fires at a real
98 % and `check_token_safety` waves through a request the provider rejects.

The anchor pins the measured `prompt_tokens` to the history it covered, keyed by
a fingerprint of that prefix. It FAILS CLOSED: any splice, eviction or
compaction of the covered span invalidates it and the pure estimate returns,
byte-identical to the pre-F6 number.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import HumanMessage


def _mm(**kwargs) -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=100000,
        session_id=kwargs.pop("session_id", "s-anchor"), **kwargs,
    )


def _fill(mm: MessageManager, n: int, tag: str = "turn") -> None:
    for i in range(n):
        mm._add_message_with_tokens(
            HumanMessage(content=f"{tag} {i}: " + ("word " * 40)), _internal=True)


# ---------------------------------------------------------------------------
# No anchor => the pre-F6 number, exactly
# ---------------------------------------------------------------------------


def test_no_anchor_is_byte_identical_to_the_pure_estimate():
    mm = _mm()
    _fill(mm, 5)
    assert mm.usage_anchor() is None

    estimate = mm.get_actual_token_count()
    # The pure sum the gauge used before F6.
    expected = mm.get_token_count()
    assert estimate == expected


def test_an_unused_manager_reports_no_anchor():
    mm = _mm()
    assert mm.usage_anchor() is None
    assert mm._usage_anchor is None


# ---------------------------------------------------------------------------
# With an anchor => measured prefix + estimated delta
# ---------------------------------------------------------------------------


def test_anchor_replaces_the_estimate_for_what_the_provider_billed():
    mm = _mm()
    _fill(mm, 6)
    mm.get_messages_for_llm(consume_ephemeral=False)

    mm.note_call_usage(output_tokens=200, input_tokens=50_000, cached_tokens=40_000)
    anchor = mm.usage_anchor()
    assert anchor is not None
    prompt_tokens, covered, hmem_included = anchor
    assert prompt_tokens == 50_000
    assert covered == len(mm.history.messages)
    assert hmem_included is False

    # Nothing appended yet: the gauge IS the provider's number.
    assert mm.get_actual_token_count() == 50_000


def test_anchor_plus_delta_after_new_turns():
    mm = _mm()
    _fill(mm, 4)
    mm.get_messages_for_llm(consume_ephemeral=False)
    mm.note_call_usage(output_tokens=100, input_tokens=30_000, cached_tokens=0)

    before = mm.history.total_tokens
    _fill(mm, 3, tag="later")
    delta = mm.history.total_tokens - before
    assert delta > 0

    assert mm.get_actual_token_count() == 30_000 + delta


def test_anchor_survives_an_append_but_not_a_splice():
    mm = _mm()
    _fill(mm, 5)
    mm.get_messages_for_llm(consume_ephemeral=False)
    mm.note_call_usage(output_tokens=10, input_tokens=12_345, cached_tokens=0)
    assert mm.usage_anchor() is not None

    _fill(mm, 1, tag="append")
    assert mm.usage_anchor() is not None, "an append must not invalidate the anchor"

    # Anything landing INSIDE the covered span does.
    mm.add_message(HumanMessage(content="spliced ahead"), position=1)
    assert mm.usage_anchor() is None, "a splice must fail the anchor closed"
    assert mm.get_actual_token_count() == mm.get_token_count()


def test_removal_inside_the_covered_span_invalidates_the_anchor():
    mm = _mm()
    _fill(mm, 6)
    mm.get_messages_for_llm(consume_ephemeral=False)
    mm.note_call_usage(output_tokens=10, input_tokens=9_000, cached_tokens=0)
    assert mm.usage_anchor() is not None

    mm.history.remove_message(2)
    assert mm.usage_anchor() is None


# ---------------------------------------------------------------------------
# Every cut drops it
# ---------------------------------------------------------------------------


def test_compaction_rebuild_resets_the_anchor():
    mm = _mm()
    _fill(mm, 8)
    mm.get_messages_for_llm(consume_ephemeral=False)
    mm.note_call_usage(output_tokens=10, input_tokens=20_000, cached_tokens=0)
    assert mm.usage_anchor() is not None

    tail = [m.message for m in list(mm.history.messages)[-2:]]
    mm._rebuild_with_summary("a summary", 5, tail)
    assert mm._usage_anchor is None
    assert mm.usage_anchor() is None


def test_emergency_prune_resets_the_anchor():
    mm = _mm()
    _fill(mm, 12)
    mm.get_messages_for_llm(consume_ephemeral=False)
    mm.note_call_usage(output_tokens=10, input_tokens=20_000, cached_tokens=0)
    assert mm.usage_anchor() is not None

    mm.emergency_context_prune()
    assert mm._usage_anchor is None


def test_batch_eviction_resets_the_anchor():
    from collections import deque

    mm = _mm(session_id="s-anchor-evict")
    maxlen = 20
    mm.history.max_messages = maxlen
    mm.history.messages = deque(list(mm.history.messages), maxlen=maxlen)

    _fill(mm, maxlen)
    mm.get_messages_for_llm(consume_ephemeral=False)
    mm.note_call_usage(output_tokens=10, input_tokens=20_000, cached_tokens=0)
    assert mm.usage_anchor() is not None

    _fill(mm, 1, tag="overflow")  # triggers the batch cut
    assert mm._usage_anchor is None


# ---------------------------------------------------------------------------
# The cache split the anchor also records
# ---------------------------------------------------------------------------


def test_last_call_usage_records_the_cache_split():
    mm = _mm()
    _fill(mm, 2)
    mm.note_call_usage(output_tokens=300, input_tokens=10_000,
                       cached_tokens=8_000, cache_creation_tokens=1_500)
    assert mm._last_call_usage == {
        "prompt_tokens": 10_000,
        "completion_tokens": 300,
        "cached_tokens": 8_000,
        "cache_creation_tokens": 1_500,
        "uncached_tokens": 2_000,
    }
