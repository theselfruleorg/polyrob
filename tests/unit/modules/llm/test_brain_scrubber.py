"""Tests for modules.llm.brain_scrubber — the shared brain-state stripper.

OR-7 (live multi-provider round, 2026-06): ROB instructs every model to emit its
brain-state as a ``{"current_state": {...}}`` JSON object in the text-content
field. On a streaming provider that content reaches the single stream funnel
(hitl_manager.stream_output → CLI / WebView / API). The old guard
(is_brain_state_content) only suppressed a chunk that was PURELY a parseable
brain object, so these real leak shapes streamed to users:
  - fenced ```json {brain} ```
  - mixed blob: {brain} + real prose reply  (DeepSeek)
  - truncated brain JSON                    (Qwen)
  - brain + trailing tool-call junk         (Kimi)
scrub_brain_blocks must remove the brain block(s) and keep only the prose.
"""
from modules.llm.brain_scrubber import scrub_brain_blocks


_WRAPPED = (
    '{"current_state": {"evaluation_previous_goal": "N/A", '
    '"memory": "User greeted me.", "next_goal": "Reply.", '
    '"reasoning": "Be friendly."}}'
)
_BARE = (
    '{"page_summary":"","evaluation_previous_goal":"Success",'
    '"memory":"x","next_goal":"Wait for input","reasoning":"y"}'
)


def test_pure_brain_block_scrubs_to_empty():
    assert scrub_brain_blocks(_WRAPPED).strip() == ""
    assert scrub_brain_blocks(_BARE).strip() == ""


def test_fenced_brain_scrubs_to_empty():
    fenced = "```json\n" + _WRAPPED + "\n```"
    assert scrub_brain_blocks(fenced).strip() == ""


def test_mixed_blob_keeps_only_prose():
    # DeepSeek shape: fenced brain, then the real reply.
    blob = "```json\n" + _WRAPPED + "\n```\n\nHey there! I'm doing great — how can I help?"
    out = scrub_brain_blocks(blob)
    assert "current_state" not in out
    assert "evaluation_previous_goal" not in out
    assert "Hey there! I'm doing great" in out


def test_prose_then_brain_keeps_prose():
    blob = "Hello! How can I help you today?\n\n" + _BARE
    out = scrub_brain_blocks(blob)
    assert "page_summary" not in out
    assert "next_goal" not in out
    assert "Hello! How can I help you today?" in out


def test_truncated_brain_scrubs_to_empty():
    # Qwen shape: stream cut mid-object → invalid JSON. Must still be removed.
    truncated = '{"page_summary":"","evaluation_previous_goal":"Success","memory":"x","next_goal":"Wait","phase":"'
    assert scrub_brain_blocks(truncated).strip() == ""


def test_brain_with_trailing_toolcall_junk_scrubs():
    blob = _WRAPPED + ' done(text="Hi there!")'
    out = scrub_brain_blocks(blob)
    assert "current_state" not in out


def test_genuine_prose_untouched():
    prose = "The capital of France is Paris. Here is a summary of the repo."
    assert scrub_brain_blocks(prose) == prose


def test_genuine_json_reply_untouched():
    # A real (non-brain) JSON answer must NOT be scrubbed.
    reply = '{"answer": 42, "unit": "things"}'
    assert scrub_brain_blocks(reply) == reply


def test_fenced_non_brain_code_untouched():
    code = "```python\nprint('hello world')\n```"
    assert scrub_brain_blocks(code) == code


def test_empty_and_none_safe():
    assert scrub_brain_blocks("") == ""
    assert scrub_brain_blocks(None) is None


def test_no_brace_fast_path():
    assert scrub_brain_blocks("just some text") == "just some text"
