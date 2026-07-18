"""Snapshot-style tests for cli.ui.blocks over REAL captured feed shapes.

Renderables are driven through a recording ``Console`` (width-pinned, no color)
so assertions are on plain text content, deterministic across environments.
"""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from cli.ui import blocks
from cli.ui.events import ErrorEvent, SessionDone, Step, normalize


def _render(renderable) -> str:
    buf = StringIO()
    console = Console(file=buf, width=80, no_color=True, highlight=False)
    console.print(renderable)
    return buf.getvalue()


# Real step.actions[] shape (§0 amendment 3), copied from the captured feed.
_REAL_STEP = {
    "type": "step",
    "step": 1,
    "data": {
        "actions": [
            {
                "action_type": "send_message",
                "name": "message",
                "service": "send",
                "params": {
                    "text": "hello",
                    "wait_for_response": False,
                    "timeout_seconds": 300,
                },
            }
        ],
        "agent_name": "executor",
        "agent_type": "Agent",
        "reasoning": "Executed: send_message(text=hello, wait_for_response=False)",
        "context": {
            "outputs": {"memory": "send_message→ok. Found: ok"},
            "metrics": {"token_count": 3370},
        },
    },
    "agent_name": "executor",
}


def test_step_header_includes_step_and_meta():
    out = _render(blocks.step_header(3, token_count=1234, duration_seconds=0.8))
    assert "Step 3" in out
    assert "1.2k tok" in out
    assert "0.8s" in out


def test_step_header_omits_missing_meta():
    out = _render(blocks.step_header(1))
    assert "Step 1" in out
    assert "tok" not in out


def test_reasoning_panel_none_for_empty():
    assert blocks.reasoning_panel("") is None
    assert blocks.reasoning_panel("   ") is None


def test_reasoning_panel_renders_text():
    out = _render(blocks.reasoning_panel("read config before editing"))
    assert "reasoning" in out
    assert "read config before editing" in out


def test_reasoning_panel_collapses_long():
    long = "\n".join(f"line {i}" for i in range(20))
    panel = blocks.reasoning_panel(long)
    out = _render(panel)
    assert "/verbose" in out
    assert "line 0" in out
    # Lines beyond the collapse threshold are dropped from the body.
    assert "line 19" not in out


def test_tool_call_line_uses_action_type_and_args():
    action = _REAL_STEP["data"]["actions"][0]
    out = _render(blocks.tool_call_line(action))
    assert "send_message" in out
    assert "hello" in out
    assert "text=" in out


def test_tool_result_suffix_success_and_fail():
    ok = _render(blocks.tool_result_suffix(success=True, duration_seconds=0.2))
    assert "0.2s" in ok
    fail = _render(blocks.tool_result_suffix(success=False, error="boom"))
    assert "boom" in fail


def test_step_block_full_render():
    ev = normalize(_REAL_STEP)
    assert isinstance(ev, Step)
    out = _render(blocks.step_block(ev))
    assert "Step 1" in out
    assert "send_message" in out
    assert "memory" in out
    # token_count pulled from data.context.metrics
    assert "3.4k tok" in out


def test_completion_panel_one_shot_shows_result():
    ev = SessionDone(success=True, total_steps=2, final_result="all done")
    out = _render(
        blocks.completion_panel(ev, tokens_total=5000, cost_estimate=0.0004,
                                elapsed_seconds=4.8, show_final_result=True)
    )
    assert "completed" in out
    assert "2 steps" in out
    assert "all done" in out
    assert "$0.0004" in out


def test_completion_panel_repl_hides_result():
    """REPL context: final result is NOT shown (rendered once via on_turn_end)."""
    ev = SessionDone(success=True, total_steps=1, final_result="secret answer")
    out = _render(blocks.completion_panel(ev, show_final_result=False))
    assert "completed" in out
    assert "secret answer" not in out


def test_error_panel_full_message():
    ev = ErrorEvent(error_message="boom went the dynamite", error_type="RuntimeError")
    out = _render(blocks.error_panel(ev))
    assert "RuntimeError" in out
    assert "boom went the dynamite" in out


def test_subagent_line_is_dim_oneliner():
    out = _render(blocks.subagent_line("researcher", 2, "looking up docs"))
    assert "└ researcher" in out
    assert "step 2" in out
    assert "looking up docs" in out


def test_agent_message_renders_with_speaker_mark():
    from cli.ui.theme import ICONS
    out = _render(blocks.agent_message("the answer is 42"))
    assert "the answer is 42" in out
    assert f"{ICONS.speaker} rob" in out


def test_no_final_message_notice_with_goal():
    from cli.ui.theme import ICONS
    out = _render(blocks.no_final_message_notice("Deliver the review"))
    assert "finished without a final message" in out
    assert "last goal:" in out
    assert "Deliver the review" in out
    assert f"{ICONS.speaker} rob" in out


def test_no_final_message_notice_without_goal():
    out = _render(blocks.no_final_message_notice(None))
    assert "finished without a final message" in out
    assert "last goal:" not in out


def test_turn_summary_line_segments():
    from cli.ui.theme import ICONS
    out = _render(blocks.turn_summary_line(
        steps=3, tools=2, tokens=14200, cost=0.0041, elapsed_seconds=28.0))
    assert f"{ICONS.speaker}" in out
    assert "3 steps" in out
    assert "2 tools" in out
    assert "14.2k tok" in out
    assert "$0.0041" in out
    assert "28s" in out


def test_turn_summary_line_omits_zero_segments():
    out = _render(blocks.turn_summary_line(steps=2, tools=0, tokens=0, cost=0.0))
    assert "2 steps" in out
    assert "tool" not in out
    assert "tok" not in out
    assert "$" not in out


def test_turn_summary_line_failed():
    from cli.ui.theme import ICONS
    out = _render(blocks.turn_summary_line(steps=1, failed=True))
    assert f"{ICONS.speaker}" in out
    assert "failed" in out


# ---------------------------------------------------------------------------
# Dialog-first: message bubble + step demotion
# ---------------------------------------------------------------------------


# A realistic long markdown send_message step (the user's screenshot scenario).
_LONG_MARKDOWN = (
    "## Repository Review: ROB\n\n"
    "Based on the analysis, here is a thorough writeup with **many** details "
    "that easily exceeds the 60-char arg-summary truncation threshold used for "
    "tool-call lines. It must appear in full inside the bubble.\n\n"
    "- point one\n- point two\n- point three\n"
)

_MSG_STEP = {
    "type": "step",
    "step": 1,
    "data": {
        "actions": [
            {
                "action_type": "send_message",
                "name": "message",
                "service": "send",
                "params": {
                    "text": _LONG_MARKDOWN,
                    "wait_for_response": True,
                    "timeout_seconds": 300,
                },
            }
        ],
        "agent_name": "executor",
        "reasoning": "Executed: send_message(text=## Repository Review: ROB, "
        "wait_for_response=True)",
        "context": {
            "outputs": {
                "memory": "send_message(text=## Repo, wait_for_response=True)→DONE"
            },
            "metrics": {"token_count": 4096},
        },
    },
    "agent_name": "executor",
}


def test_agent_message_renders_full_markdown_no_truncation():
    from cli.ui.theme import ICONS
    out = _render(blocks.agent_message(_LONG_MARKDOWN))
    assert "Repository Review" in out
    assert "point three" in out
    assert "…" not in out  # never truncated
    assert f"{ICONS.speaker}" in out


def test_step_block_skips_echo_reasoning_and_send_message_line():
    """Non-verbose step_block demotes the echo reasoning + send_message line."""
    ev = normalize(_MSG_STEP)
    out = _render(blocks.step_block(ev, verbose=False))
    # The post-hoc echo reasoning is gone, the send_message tool line is gone.
    assert "Executed:" not in out
    assert "send_message" not in out
    # The echo memory line is hidden too.
    assert "memory" not in out


def test_step_block_verbose_reveals_everything():
    ev = normalize(_MSG_STEP)
    out = _render(blocks.step_block(ev, verbose=True))
    # Verbose shows the echo reasoning (dim), the memory line, and the
    # suppressed send_message tool line.
    assert "Executed:" in out
    assert "send_message" in out
    assert "memory" in out


def test_step_block_mixed_keeps_non_message_tool_line():
    """A mixed step keeps non-message tool lines while suppressing the msg line."""
    mixed = {
        "type": "step",
        "step": 2,
        "data": {
            "actions": [
                {"action_type": "task_todo_add", "name": "task_todo_add",
                 "service": "task", "params": {"content": "do the thing"}},
                {"action_type": "send_message", "name": "message",
                 "service": "send", "params": {"text": "working on it"}},
            ],
            "agent_name": "executor",
            "reasoning": "I will add a todo and notify the user.",
            "context": {"outputs": {"memory": ""}, "metrics": {"token_count": 100}},
        },
        "agent_name": "executor",
    }
    out = _render(blocks.step_block(normalize(mixed), verbose=False))
    assert "task_todo_add" in out          # non-message tool line stays
    assert "send_message" not in out       # message line suppressed (bubble)
    assert "I will add a todo" in out      # genuine reasoning stays


def test_user_message_echoes_text_with_caret():
    out = _render(blocks.user_message("what is 2+2?"))
    assert "what is 2+2?" in out
    assert "❯" in out


def test_user_message_blank_returns_none():
    assert blocks.user_message("   ") is None
    assert blocks.user_message("") is None


# ---------------------------------------------------------------------------
# Sub-agent lane glyph migration (Task 7)
# ---------------------------------------------------------------------------


def test_subagent_line_uses_tree_glyph():
    line = blocks.subagent_line("researcher", 3, "scanning docs")
    text = line.plain
    assert text.startswith("  └ researcher")
    assert "step 3" in text and "scanning docs" in text


def test_subagent_line_no_summary():
    text = blocks.subagent_line("researcher", 2).plain
    assert text == "  └ researcher · step 2"


def test_subagent_line_collapses_multiline_summary():
    text = blocks.subagent_line("r", 1, "line1\nline2").plain
    tree_idx = text.index("└")
    assert "\n" not in text[tree_idx:]


def test_speaker_glyphs_come_from_theme():
    from cli.ui.theme import ICONS
    assert blocks.agent_message("hi") is not None  # existing golden covers content
    assert blocks.working_notice().plain.startswith(f"{ICONS.working} ")


def test_agent_message_has_no_trailing_blank_line():
    # Blank-BEFORE-only rhythm: the gap below the last block is owned by the
    # pinned region's spacer row, not the bubble.
    out = _render(blocks.agent_message("hi"))
    assert out.startswith("\n")
    assert not out.endswith("\n\n")


def test_turn_summary_line_has_leading_blank():
    out = _render(blocks.turn_summary_line(steps=3, tools=2, tokens=14200, cost=0.004))
    assert out.startswith("\n")
