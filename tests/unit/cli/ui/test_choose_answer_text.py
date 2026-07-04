"""D6: dialog.choose_answer_text — the SSOT for the OR-7 answer-text selection.

Both renderers' turn-end paths inlined the identical rule; it now lives once here.
"""

from __future__ import annotations

from cli.ui import dialog


def test_prefers_clean_answer_over_stream():
    # A real parsed answer wins over raw streamed brain-state.
    assert dialog.choose_answer_text("the real answer", '{"current_state": ...}') == "the real answer"


def test_falls_back_to_stream_when_answer_blank():
    assert dialog.choose_answer_text("", "streamed text") == "streamed text"
    assert dialog.choose_answer_text("   ", "streamed text") == "streamed text"


def test_falls_back_to_answer_when_stream_blank():
    assert dialog.choose_answer_text("answer", "") == "answer"
    assert dialog.choose_answer_text("answer", "   ") == "answer"


def test_plumbing_answer_does_not_win_over_stream():
    # A plumbing receipt is not the agent's voice → prefer the stream.
    plumbing = next(iter(dialog._PLUMBING_STRINGS))
    assert dialog.choose_answer_text(plumbing, "streamed") == "streamed"


def test_both_blank_returns_answer():
    assert dialog.choose_answer_text("", "") == ""
