"""UP-07 Step 7.3 — adapter scrubs content at the AIMessage seam, flag-gated, safe."""
import logging

from modules.llm.adapters import LLMClientAdapter, think_scrubber_enabled


def _adapter():
    a = object.__new__(LLMClientAdapter)
    a._logger = logging.getLogger("scrub-adapter-test")
    a._client = type("C", (), {"model_type": "kimi-k2"})()
    return a


def test_scrubs_leaked_block():
    a = _adapter()
    out = a._scrub_content("<think>secret reasoning</think>The answer.")
    assert "secret" not in out
    assert "The answer." in out


def test_flag_off_byte_identical(monkeypatch):
    monkeypatch.setenv("THINK_SCRUBBER_ENABLED", "0")
    a = _adapter()
    raw = "<think>secret</think>answer"
    assert a._scrub_content(raw) == raw
    assert think_scrubber_enabled() is False


def test_non_string_untouched():
    a = _adapter()
    blocks = [{"type": "text", "text": "hi"}]
    assert a._scrub_content(blocks) is blocks
    assert a._scrub_content(None) is None


def test_no_tag_fast_path_unchanged():
    a = _adapter()
    assert a._scrub_content("plain answer, no tags") == "plain answer, no tags"


def test_default_enabled(monkeypatch):
    monkeypatch.delenv("THINK_SCRUBBER_ENABLED", raising=False)
    assert think_scrubber_enabled() is True
