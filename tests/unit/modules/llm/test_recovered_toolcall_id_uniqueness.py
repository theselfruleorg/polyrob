"""P0-4: recovered tool-call ids must be GLOBALLY unique, not per-response.

Kimi/NIM textual recovery (``parse_kimi_tool_calls`` / ``recover_textual_tool_calls``
in modules/llm/openrouter_client.py) used to mint deterministic per-response ids
(``call_{i}_{idx}`` / ``call_txt_{i}``). Two recovery turns in one session therefore
produced DUPLICATE ids across history. Downstream,
``tool_message_repair.repair_tool_message_pairs`` keys ``tool_msg_map`` by id over
the WHOLE history (last write wins) and ``del``s the id on first use — so step 1's
AIMessage got paired with step 2's tool result, and step 2 got a fabricated
"[ERROR: No response recorded...]" placeholder.

Textual leaks fire on ~20-25% of Kimi/NIM turns, so two recovery turns per session
are routine. These tests pin the fix: every recovered id is globally unique.
"""

import pytest

from modules.llm.openrouter_client import (
    parse_kimi_tool_calls,
    recover_textual_tool_calls,
)

# Two parallel Kimi pipe-token calls in one response (per-function idx both 0).
_KIMI_LEAK = (
    '<|tool_call_begin|> functions.read:0 <|tool_call_argument_begin|> '
    '{"path": "a.txt"} <|tool_call_end|> '
    '<|tool_call_begin|> functions.write:0 <|tool_call_argument_begin|> '
    '{"path": "b.txt"} <|tool_call_end|>'
)

# Two textual XML-invoke calls in one response.
_XML_LEAK = (
    '<invoke name="read"><parameter name="path">a.txt</parameter></invoke>'
    '<invoke name="write"><parameter name="path">b.txt</parameter></invoke>'
)


def _ids(calls):
    return [c["id"] for c in calls]


# ---------------------------------------------------------------------------
# Within one response: each call gets a distinct id.
# ---------------------------------------------------------------------------

def test_kimi_recovery_ids_distinct_within_one_response():
    calls = parse_kimi_tool_calls(_KIMI_LEAK)
    assert len(calls) == 2
    ids = _ids(calls)
    assert len(set(ids)) == 2, f"ids must be distinct within a response, got {ids}"


def test_textual_recovery_ids_distinct_within_one_response():
    _cleaned, calls = recover_textual_tool_calls(_XML_LEAK)
    assert len(calls) == 2
    ids = _ids(calls)
    assert len(set(ids)) == 2, f"ids must be distinct within a response, got {ids}"


# ---------------------------------------------------------------------------
# Across responses: two consecutive recoveries produce DISJOINT id sets.
# (This is the P0-4 bug: the old ids were deterministic per-response.)
# ---------------------------------------------------------------------------

def test_kimi_recovery_ids_disjoint_across_two_recoveries():
    first = set(_ids(parse_kimi_tool_calls(_KIMI_LEAK)))
    second = set(_ids(parse_kimi_tool_calls(_KIMI_LEAK)))
    assert first and second
    assert first.isdisjoint(second), (
        f"two recovery turns must never reuse ids: {first & second}"
    )


def test_textual_recovery_ids_disjoint_across_two_recoveries():
    _c1, calls1 = recover_textual_tool_calls(_XML_LEAK)
    _c2, calls2 = recover_textual_tool_calls(_XML_LEAK)
    first, second = set(_ids(calls1)), set(_ids(calls2))
    assert first and second
    assert first.isdisjoint(second), (
        f"two recovery turns must never reuse ids: {first & second}"
    )


def test_recovered_ids_are_provider_safe_strings():
    for calls in (
        parse_kimi_tool_calls(_KIMI_LEAK),
        recover_textual_tool_calls(_XML_LEAK)[1],
    ):
        for c in calls:
            cid = c["id"]
            assert isinstance(cid, str)
            assert cid.startswith("call_")
            assert 0 < len(cid) <= 40  # OpenAI-shape ids are short strings
            assert all(ch.isalnum() or ch == "_" for ch in cid)


# ---------------------------------------------------------------------------
# Integration-ish: the exact downstream failure this fixes. Two recovery turns
# (previously colliding ids) + their ToolMessages must survive
# repair_tool_message_pairs with results still paired to the RIGHT AIMessage
# and no fabricated placeholder.
# ---------------------------------------------------------------------------

def test_repair_keeps_two_recovery_turns_correctly_paired():
    from modules.llm.messages import AIMessage, ToolMessage
    from agents.task.agent.message_manager.tool_message_repair import (
        repair_tool_message_pairs,
    )

    # Simulate two consecutive recovery turns in one session.
    _c1, calls_step1 = recover_textual_tool_calls(
        '<invoke name="read"><parameter name="path">step1.txt</parameter></invoke>'
    )
    _c2, calls_step2 = recover_textual_tool_calls(
        '<invoke name="read"><parameter name="path">step2.txt</parameter></invoke>'
    )
    assert len(calls_step1) == 1 and len(calls_step2) == 1

    id1, id2 = calls_step1[0]["id"], calls_step2[0]["id"]

    ai1 = AIMessage(content="step 1", tool_calls=calls_step1)
    tm1 = ToolMessage(content="RESULT-STEP-1", tool_call_id=id1)
    ai2 = AIMessage(content="step 2", tool_calls=calls_step2)
    tm2 = ToolMessage(content="RESULT-STEP-2", tool_call_id=id2)

    repaired, _report = repair_tool_message_pairs([ai1, tm1, ai2, tm2])

    # No fabricated "[ERROR: No response recorded...]" placeholder anywhere.
    for msg in repaired:
        assert "[ERROR: No response recorded" not in str(
            getattr(msg, "content", "")
        ), "repair fabricated a placeholder — ids collided across steps"

    # Each AIMessage is immediately followed by ITS OWN result.
    by_content = {getattr(m, "content", None): i for i, m in enumerate(repaired)}
    i_ai1, i_ai2 = by_content["step 1"], by_content["step 2"]
    assert getattr(repaired[i_ai1 + 1], "tool_call_id", None) == id1
    assert repaired[i_ai1 + 1].content == "RESULT-STEP-1"
    assert getattr(repaired[i_ai2 + 1], "tool_call_id", None) == id2
    assert repaired[i_ai2 + 1].content == "RESULT-STEP-2"
