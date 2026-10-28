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
