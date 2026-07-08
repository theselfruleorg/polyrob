"""UP-07 Step 7.1 — golden tests for the ported StreamingThinkScrubber + scrub_think_blocks.

Ported semantics: closed-pair anywhere, boundary-gated open, partial hold-back across
deltas, orphan-close strip, flush-discards-open, case-insensitive. Plus the UP-07-specific
risk case: brain-state JSON fully wrapped in <think> is intentionally stripped.
"""
from modules.llm.think_scrubber import StreamingThinkScrubber, scrub_think_blocks


def _all(scrubber, *deltas):
    out = "".join(scrubber.feed(d) for d in deltas)
    return out + scrubber.flush()


def test_closed_pair_removed_anywhere():
    # Preceding text (incl. its trailing space) is preserved; only the block is removed.
    assert scrub_think_blocks("before <think>secret</think>after") == "before after"
    assert "secret" not in scrub_think_blocks("before <think>secret</think>after")


def test_streaming_split_regression():
    # The Reference regression: split across deltas must all be suppressed.
    s = StreamingThinkScrubber()
    assert _all(s, "<think>", "Let me check their config", "</think>") == ""


def test_open_tag_not_at_boundary_kept():
    # Prose that mentions the tag mid-line must NOT be stripped.
    assert scrub_think_blocks("use <think> tags here") == "use <think> tags here"


def test_unterminated_open_at_boundary_flush_discards():
    s = StreamingThinkScrubber()
    out = s.feed("<think>partial reasoning that never closes")
    assert out == ""
    assert s.flush() == ""  # discarded, not leaked


def test_partial_tag_across_deltas():
    s = StreamingThinkScrubber()
    a = s.feed("abc <thin")          # holds back "<thin"
    b = s.feed("k>secret</think> done")
    assert (a + b + s.flush()).strip() == "abc  done".strip()
    assert "secret" not in (a + b)


def test_orphan_close_tag_stripped_at_boundary():
    # A standalone / leading orphan close tag (block boundary) is noise → stripped.
    assert scrub_think_blocks("</think>bar") == "bar"
    assert scrub_think_blocks("</think>\nThe answer is 42.") == "The answer is 42."


def test_orphan_close_tag_midprose_preserved():
    # Regression: a close tag the assistant legitimately wrote mid-prose (docs,
    # debugging help, code-gen about reasoning tags) must NOT be deleted — doing so
    # silently corrupts real output.
    assert scrub_think_blocks("foo</think>bar") == "foo</think>bar"
    assert scrub_think_blocks("To debug, look for </think> in logs.") == (
        "To debug, look for </think> in logs."
    )


def test_case_insensitive():
    assert scrub_think_blocks("<THINKING>x</THINKING>y") == "y"
    assert scrub_think_blocks("a<Reasoning>z</Reasoning>") == "a"


def test_plain_text_unchanged():
    assert scrub_think_blocks("just normal text") == "just normal text"
    assert scrub_think_blocks("") == ""


def test_no_tag_fast_path_no_false_strip():
    assert scrub_think_blocks("a < b and c < d") == "a < b and c < d"


def test_brain_state_wrapped_in_think_is_stripped():
    # UP-07 risk case: a reasoning model that wraps its brain-state JSON inside <think>
    # has the WHOLE block stripped (intended). Such a model must carry brain-state in
    # tool_calls, not text — documented behavior, asserted here so it's not a surprise.
    content = '<think>{"memory": "x", "next_goal": "y"}</think>'
    assert scrub_think_blocks(content) == ""


def test_reasoning_then_real_answer():
    content = "<think>\nlet me think\n</think>\nThe answer is 42."
    assert scrub_think_blocks(content).strip() == "The answer is 42."


def test_p2_13_nested_same_variant_no_leak():
    """P2-13: a nested same-variant reasoning block must be removed IN FULL (depth-
    aware close matching), not truncated at the first inner close leaking the tail."""
    from modules.llm.think_scrubber import scrub_think_blocks
    assert scrub_think_blocks(
        "<think>a<think>b</think>secret leftover</think>ok") == "ok"
    assert scrub_think_blocks(
        "<think>outer<think>inner</think>more</think>visible") == "visible"


def test_p2_13_non_nested_unchanged():
    """Regression: a simple closed pair still strips normally."""
    from modules.llm.think_scrubber import scrub_think_blocks
    assert scrub_think_blocks("<think>x</think>after") == "after"
    assert scrub_think_blocks("before<think>x</think>after") == "beforeafter"
