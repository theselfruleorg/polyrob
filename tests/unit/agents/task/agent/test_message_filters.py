"""Unit tests for the extracted FiltersMixin (PR10).

Exercises _filter_sensitive_data in isolation via a tiny host composing the
mixin — no LLM/token/storage state required.
"""

from agents.task.agent.messages.filters import FiltersMixin
from modules.llm.messages import HumanMessage


class _Host(FiltersMixin):
    def __init__(self, sensitive_data=None):
        self.sensitive_data = sensitive_data or {}


def test_filter_redacts_string_content():
    host = _Host(sensitive_data={"api_key": "sk-supersecret"})
    msg = HumanMessage(content="my key is sk-supersecret ok")
    out = host._filter_sensitive_data(msg)
    assert "sk-supersecret" not in out.content
    assert "<secret>api_key</secret>" in out.content


def test_filter_does_not_mutate_original():
    host = _Host(sensitive_data={"api_key": "sk-supersecret"})
    msg = HumanMessage(content="sk-supersecret")
    out = host._filter_sensitive_data(msg)
    # original untouched (deep copy), filtered copy redacted
    assert msg.content == "sk-supersecret"
    assert out.content == "<secret>api_key</secret>"


def test_filter_noop_without_sensitive_data():
    host = _Host(sensitive_data={})
    msg = HumanMessage(content="nothing secret here")
    out = host._filter_sensitive_data(msg)
    assert out.content == "nothing secret here"


# --- Phase 0.5: pattern backstop for UNregistered secrets -------------------

def test_filter_redacts_unregistered_provider_key(monkeypatch):
    """An sk- key never registered in sensitive_data is still redacted before it
    can persist to message_history.json / compaction checkpoints."""
    monkeypatch.delenv("HISTORY_SECRET_SCRUB", raising=False)
    host = _Host(sensitive_data={})  # nothing registered
    msg = HumanMessage(content="leaked OPENAI key sk-abc123DEF456ghi789JKLmno here")
    out = host._filter_sensitive_data(msg)
    assert "sk-abc123DEF456ghi789JKLmno" not in out.content
    assert "here" in out.content


def test_filter_redacts_unregistered_pem_block(monkeypatch):
    monkeypatch.delenv("HISTORY_SECRET_SCRUB", raising=False)
    host = _Host(sensitive_data={})
    pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n"
           "-----END RSA PRIVATE KEY-----")
    msg = HumanMessage(content=f"cat id_rsa\n{pem}")
    out = host._filter_sensitive_data(msg)
    assert "MIIEpAIBAAKCAQEA" not in out.content
    assert "PRIVATE KEY" not in out.content


def test_filter_pattern_backstop_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HISTORY_SECRET_SCRUB", "off")
    host = _Host(sensitive_data={})
    msg = HumanMessage(content="sk-abc123DEF456ghi789JKLmno")
    out = host._filter_sensitive_data(msg)
    assert out.content == "sk-abc123DEF456ghi789JKLmno"  # untouched when off


def test_filter_does_not_redact_working_hex_hash(monkeypatch):
    """Conservative backstop must not corrupt legitimate working content."""
    monkeypatch.delenv("HISTORY_SECRET_SCRUB", raising=False)
    host = _Host(sensitive_data={})
    sha = "a" * 40
    msg = HumanMessage(content=f"compare against commit {sha}")
    out = host._filter_sensitive_data(msg)
    assert sha in out.content


def test_merge_successive_text_uses_separator():
    """B20: merging two successive text messages must keep a boundary, not glue
    '...end''start...' into one run."""
    from modules.llm.messages import HumanMessage
    host = _Host()
    merged = host.merge_successive_messages(
        [HumanMessage(content="first line"), HumanMessage(content="second line")],
        HumanMessage,
    )
    assert len(merged) == 1
    assert merged[0].content == "first line\nsecond line"
    assert "linesecond" not in merged[0].content


# --- F16(i): the dedup back-reference is CONTENT-addressed, not positional ---

def _dup_payload() -> str:
	# Must clear DEDUP_MIN_CHARS (160) for the dedup pass to fire at all.
	return "IDENTICAL TOOL OUTPUT. " * 12


def test_dedup_backreference_bytes_are_position_independent():
    """Prepending a message must not re-word the pointer to the first result.

    The pointer used to carry the first occurrence's list POSITION, so any
    shift (a left-eviction, a restore, an insert) re-serialized the same
    unchanged conversation to different bytes and colded the provider cache
    from that message onward.
    """
    from agents.task.agent.messages.filters import dedup_tool_results
    from modules.llm.messages import ToolMessage

    payload = _dup_payload()
    pair = [
        ToolMessage(content=payload, tool_call_id="call_1"),
        ToolMessage(content=payload, tool_call_id="call_2"),
    ]

    before = dedup_tool_results(list(pair))
    after = dedup_tool_results([HumanMessage(content="a note that lands ahead")] + list(pair))

    assert before[1].content != payload, "the dedup pass did not fire"
    assert after[2].content == before[1].content, (
        "the back-reference changed bytes because a message moved:\n"
        f"  was: {before[1].content}\n  now: {after[2].content}"
    )


def test_dedup_backreference_names_the_digest():
    from agents.task.agent.messages.filters import dedup_tool_results
    from modules.llm.messages import ToolMessage
    import hashlib

    payload = _dup_payload()
    out = dedup_tool_results([
        ToolMessage(content=payload, tool_call_id="call_1"),
        ToolMessage(content=payload, tool_call_id="call_2"),
    ])
    digest = hashlib.md5(payload.encode("utf-8", "ignore")).hexdigest()[:12]
    assert digest in out[1].content
    assert "#" not in out[1].content, "no positional index may survive in the pointer"


# --- F16(iii): the deepseek-reasoner merge is scoped to ONE layer at a time ---

def _deepseek_mm():
    """A real MessageManager whose model name triggers the merge path."""
    from unittest.mock import MagicMock
    from agents.task.agent.message_manager.service import MessageManager
    from agents.task.agent.prompts import SystemPrompt

    llm = MagicMock()
    llm.model_name = "deepseek-reasoner"
    return MessageManager(
        llm=llm, task="t", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=40000,
        session_id="s-deepseek",
    )


def test_deepseek_merge_leaves_the_foundation_bytes_tail_independent():
    """The foundation's merged form must not change when the tail changes."""
    from tests.support.prefix_cache import serialize_messages

    mm = _deepseek_mm()
    mm.set_skill_message("Use tool X to accomplish Y.")

    mm.append_user_turn("first turn")
    first = serialize_messages(mm.get_messages_for_llm(consume_ephemeral=False))
    # Anything beyond the seam message must be untouched by a later append.
    mm.add_tool_call_pair_atomic(
        ai_content="working",
        tool_calls=[{"id": "c1", "name": "noop", "args": {}}],
        tool_responses=[("c1", "done")],
    )
    second = serialize_messages(mm.get_messages_for_llm(consume_ephemeral=False))

    # The foundation (everything before the first conversation turn) is identical.
    n_foundation = len(first) - 1
    assert n_foundation >= 1
    assert first[:n_foundation] == second[:n_foundation], (
        "the deepseek merge rewrote the pinned foundation when the tail grew"
    )


def test_deepseek_merge_leaves_no_successive_same_role_pair():
    """The provider contract the merge exists for must still hold."""
    from modules.llm.messages import AIMessage, HumanMessage

    mm = _deepseek_mm()
    mm.set_skill_message("Use tool X.")
    mm.append_user_turn("first turn")
    msgs = mm.get_messages_for_llm(consume_ephemeral=False)

    for prev, cur in zip(msgs, msgs[1:]):
        same_human = isinstance(prev, HumanMessage) and isinstance(cur, HumanMessage)
        same_ai = (isinstance(prev, AIMessage) and isinstance(cur, AIMessage)
                   and not getattr(prev, "tool_calls", None))
        assert not same_human and not same_ai, (
            f"successive same-role pair survived the merge: "
            f"{type(prev).__name__} -> {type(cur).__name__}"
        )
