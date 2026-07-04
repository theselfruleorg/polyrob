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
