"""Headless tests for RichRenderer driven over a StringIO console."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from cli.ui.events import normalize
from cli.ui.rich_renderer import RichRenderer
from cli.ui.state import SessionState


def _renderer(one_shot: bool = False):
    buf = StringIO()
    console = Console(file=buf, width=80, no_color=True, highlight=False)
    state = SessionState()
    return RichRenderer(state, console=console, one_shot=one_shot), state, buf


_REAL_STEP = {
    "type": "step",
    "step": 1,
    "data": {
        "actions": [
            {"action_type": "send_message", "name": "message", "service": "send",
             "params": {"text": "hello", "wait_for_response": False}}
        ],
        "agent_name": "executor",
        "reasoning": "Executed: send_message",
        "context": {"outputs": {"memory": "m"}, "metrics": {"token_count": 3370}},
    },
    "agent_name": "executor",
}


def test_step_event_renders_message_bubble():
    """A message-only step in the REPL renders the bubble, not the step block.

    Dialog-first: the send_message text is the hero; the ``→ send_message(...)``
    tool line and step scaffolding are suppressed.
    """
    r, _, buf = _renderer()
    r.on_event(normalize(_REAL_STEP))
    out = buf.getvalue()
    # The message text appears as the rob bubble...
    assert "hello" in out
    assert "rob" in out
    # ...but the demoted scaffolding does not (REPL message-only chat turn).
    assert "Step 1" not in out
    assert "→ send_message" not in out


def test_error_event_renders_panel():
    r, _, buf = _renderer()
    r.on_event(normalize({"type": "error",
                          "data": {"error_message": "kaboom",
                                   "error_type": "ValueError"}}))
    out = buf.getvalue()
    assert "kaboom" in out
    assert "ValueError" in out


def test_session_done_default_is_silent():
    """A successful SessionDone is trace: the activity summary covers it."""
    r, _, buf = _renderer(one_shot=False)
    r.on_event(normalize({"type": "session_completion",
                          "data": {"success": True, "total_steps": 1,
                                   "metrics": {"final_result": "the answer"}}}))
    assert buf.getvalue() == ""


def test_session_done_failed_surfaces_error_message():
    """Dialog layer: a failed session must explain itself, by default."""
    r, _, buf = _renderer(one_shot=False)
    r.on_event(normalize({"type": "session_completion",
                          "data": {"success": False, "total_steps": 0,
                                   "error_message": "FATAL: quota exceeded"}}))
    out = buf.getvalue()
    assert "FATAL: quota exceeded" in out
    assert "session failed" in out


def test_session_done_verbose_repl_panel_hides_result():
    r, _, buf = _renderer(one_shot=False)
    r.verbose = True
    r.on_event(normalize({"type": "session_completion",
                          "data": {"success": True, "total_steps": 1,
                                   "metrics": {"final_result": "the answer"}}}))
    out = buf.getvalue()
    assert "completed" in out
    assert "the answer" not in out  # double-render guard


def test_session_done_verbose_one_shot_shows_result():
    r, _, buf = _renderer(one_shot=True)
    r.verbose = True
    r.on_event(normalize({"type": "session_completion",
                          "data": {"success": True, "total_steps": 1,
                                   "metrics": {"final_result": "the answer"}}}))
    out = buf.getvalue()
    assert "the answer" in out


def test_on_turn_start_echoes_user_message():
    """Bug C: the user's turn lands in the transcript the instant they submit.

    In the persistent box the user's own line must appear in scrollback BEFORE
    the agent replies (Claude-Code parity) — previously only PlainRenderer
    echoed it, so REPL turns vanished until the answer arrived.
    """
    r, _, buf = _renderer()
    r.on_turn_start("what is 2+2?")
    out = buf.getvalue()
    assert "what is 2+2?" in out


def test_on_turn_start_skips_echo_for_blank_turn():
    """A blank/whitespace turn isn't echoed (the loop never submits one, but the
    renderer must not paint an empty caret line if it sees one)."""
    r, _, buf = _renderer()
    r.on_turn_start("   ")
    assert buf.getvalue() == ""


def test_on_turn_end_prints_answer_once():
    r, _, buf = _renderer()
    r.on_turn_start("hi")
    r.on_turn_end("here is the reply")
    out = buf.getvalue()
    assert out.count("here is the reply") == 1


# WS-3.1: a run that ends on a brain-state turn (agent never delivered a real
# message) must NOT present the telemetry dump as the answer — surface an
# explicit "finished without a final message" notice instead.
_TERMINAL_BRAIN = (
    '{"current_state": {"evaluation_previous_goal":"Success",'
    '"memory":"Reviewed the folder.","next_goal":"Deliver the review",'
    '"reasoning":"All files read."}}'
)


def test_on_turn_end_brain_state_shows_no_final_message_notice():
    r, _, buf = _renderer()
    r.on_turn_start("review this folder")
    r.on_turn_end(_TERMINAL_BRAIN)
    out = buf.getvalue()
    assert "finished without a final message" in out
    # The distilled goal is shown as context...
    assert "Deliver the review" in out
    # ...but the raw brain-state JSON is NEVER dumped as the agent's voice.
    assert "current_state" not in out
    assert '{"' not in out


def test_on_turn_end_brain_state_with_stray_tokens_notice():
    # The c1c81b26 shape: brain JSON + trailing kimi control tokens.
    r, _, buf = _renderer()
    r.on_turn_start("review this folder")
    r.on_turn_end(_TERMINAL_BRAIN + " <|tool_call_end|> <|tool_calls_section_end|>")
    out = buf.getvalue()
    assert "finished without a final message" in out
    assert "<|tool_call" not in out
    assert "current_state" not in out


def test_sub_agent_step_renders_oneliner_in_verbose():
    """Sub-agent step (different agent_name) → dim one-liner under verbose,
    silence by default — its text is never "rob" speaking.

    Uses the REAL formatter shape: step dicts carry agent_name (never agent_id).
    Main agent is registered via a real-shaped agent_registration event first.
    """
    r, state, buf = _renderer()
    r.verbose = True

    # Register the main agent with the real captured feed shape:
    # agent_registration carries data.agent_id + data.agent_name.
    state.update(normalize({
        "type": "agent_registration",
        "data": {
            "agent_id": "executor_2fe9b809-24ba-4894-9546-8891be935988",
            "agent_name": "executor",
            "agent_type": "Agent",
            "model_name": "gemini-2.5-flash",
            "task": "test task",
        },
    }))

    # A step from a different agent_name is a sub-agent → dim one-liner.
    # Real step shape: agent_name in data + top-level; NO agent_id in step dict.
    sub_step = {
        "type": "step",
        "step": 2,
        "agent_name": "researcher",
        "data": {
            "agent_name": "researcher",
            "agent_type": "Agent",
            "reasoning": "searching docs",
            "actions": [],
            "context": {"outputs": {"memory": ""}},
        },
    }
    r.on_event(normalize(sub_step))
    out = buf.getvalue()
    assert "researcher" in out
    # Sub-agent steps don't print a full reasoning/Step panel.
    assert "Step 2" not in out


def test_main_agent_step_renders_bubble_not_subagent_line():
    """Main agent's own message step is NOT collapsed to a sub-agent one-liner.

    It renders the dialog bubble (with the message text), not a ``+ executor
    step 1`` line.
    """
    r, state, buf = _renderer()

    # Register main agent (same shape as real captured feed).
    state.update(normalize({
        "type": "agent_registration",
        "data": {
            "agent_id": "executor_2fe9b809-24ba-4894-9546-8891be935988",
            "agent_name": "executor",
        },
    }))

    # Step from the main agent itself — uses the real captured shape.
    r.on_event(normalize(_REAL_STEP))
    out = buf.getvalue()
    assert "hello" in out
    assert "rob" in out
    # Not collapsed to a sub-agent one-liner.
    assert "step 1" not in out.lower() or "rob" in out


def test_session_start_emits_no_block():
    r, _, buf = _renderer()
    r.on_event(normalize({"type": "session_start",
                          "data": {"task": "t", "model_name": "m"}}))
    assert buf.getvalue() == ""


# ---------------------------------------------------------------------------
# Phase 3 — streaming box + double-render guard
# ---------------------------------------------------------------------------


def test_stream_chunks_then_turn_end_renders_answer_once():
    """With stream chunks, on_turn_end finalizes the box (no duplicate)."""
    r, _, buf = _renderer()
    r.on_turn_start("hi")
    r.on_stream_delta("streamed ")
    r.on_stream_delta("answer")
    r.on_turn_end("streamed answer")  # answer param matches the streamed text
    out = buf.getvalue()
    assert out.count("streamed answer") == 1


def test_single_chunk_and_multi_chunk_render_identically():
    """1-chunk and N-chunk delivery produce the same finalized answer text."""
    r1, _, buf1 = _renderer()
    r1.on_turn_start("q")
    r1.on_stream_delta("the whole answer")
    r1.on_turn_end("the whole answer")

    r2, _, buf2 = _renderer()
    r2.on_turn_start("q")
    for ch in "the whole answer":
        r2.on_stream_delta(ch)
    r2.on_turn_end("the whole answer")

    assert "the whole answer" in buf1.getvalue()
    assert "the whole answer" in buf2.getvalue()
    assert buf1.getvalue().count("the whole answer") == 1
    assert buf2.getvalue().count("the whole answer") == 1


def test_no_stream_chunks_prints_answer_param_once():
    """Without any deltas (streaming off), on_turn_end prints the answer once."""
    r, _, buf = _renderer()
    r.on_turn_start("hi")
    r.on_turn_end("non-streamed reply")
    out = buf.getvalue()
    assert out.count("non-streamed reply") == 1


def test_clean_answer_param_wins_over_streamed_box():
    """OR-7: the clean parsed action text (answer param) is canonical; the raw
    streamed box is internal telemetry for many providers and must NOT win."""
    r, _, buf = _renderer()
    r.on_turn_start("hi")
    r.on_stream_delta("internal streamed telemetry")
    r.on_turn_end("The real parsed answer")
    out = buf.getvalue()
    assert "The real parsed answer" in out
    assert "internal streamed telemetry" not in out


def test_streamed_box_used_when_no_clean_answer():
    """OR-7 fallback: when no clean answer exists (empty/plumbing param), the
    streamed box is still surfaced — never lose a real tool-free reply."""
    r, _, buf = _renderer()
    r.on_turn_start("hi")
    r.on_stream_delta("the only content we have")
    r.on_turn_end("")  # no parsed action text
    out = buf.getvalue()
    assert "the only content we have" in out


def test_turn_start_resets_box_between_turns():
    """A new turn starts a fresh box — no carryover from the previous turn."""
    r, _, buf = _renderer()
    r.on_turn_start("q1")
    r.on_stream_delta("first answer")
    r.on_turn_end("first answer")

    r.on_turn_start("q2")
    r.on_turn_end("second answer")  # no chunks this turn → prints param
    out = buf.getvalue()
    assert out.count("first answer") == 1
    assert out.count("second answer") == 1


# ---------------------------------------------------------------------------
# Dialog-first — message bubble + plumbing suppression + dedupe
# ---------------------------------------------------------------------------


_LONG_MD = "## Repository Review\n\n" + ("detail " * 40) + "\n- a\n- b\n"

_MSG_STEP = {
    "type": "step",
    "step": 1,
    "data": {
        "actions": [
            {"action_type": "send_message", "name": "message", "service": "send",
             "params": {"text": _LONG_MD, "wait_for_response": True}}
        ],
        "agent_name": "executor",
        "reasoning": "Executed: send_message(text=## Repository Review, "
        "wait_for_response=True)",
        "context": {"outputs": {"memory": "send_message(text=##)→DONE"},
                    "metrics": {"token_count": 4096}},
    },
    "agent_name": "executor",
}

_PLUMBING = "Message sent to user. Task paused - will resume when user responds."


def test_message_step_renders_full_bubble_no_tool_line():
    r, _, buf = _renderer()
    r.on_turn_start("review the repo")
    r.on_event(normalize(_MSG_STEP))
    out = buf.getvalue()
    assert "Repository Review" in out          # full markdown text shown
    assert "…" not in out                      # never truncated
    assert "→ send_message" not in out          # tool line suppressed
    assert "Executed:" not in out               # echo reasoning skipped
    assert "Step 1" not in out                  # message-only REPL chat turn


def test_turn_end_suppresses_plumbing_after_bubble():
    r, _, buf = _renderer()
    r.on_turn_start("review the repo")
    r.on_event(normalize(_MSG_STEP))
    before = buf.getvalue()
    r.on_turn_end(_PLUMBING)
    after = buf.getvalue()
    # on_turn_end added nothing — the bubble already said it.
    assert after == before
    assert "Task paused" not in after


def test_turn_end_suppresses_plumbing_without_bubble():
    r, _, buf = _renderer()
    r.on_turn_start("hi")
    r.on_turn_end(_PLUMBING)
    assert "Task paused" not in buf.getvalue()


def test_turn_end_renders_real_done_answer_when_no_bubble():
    r, _, buf = _renderer()
    r.on_turn_start("what is 2+2?")
    r.on_turn_end("The answer is 4.")
    out = buf.getvalue()
    assert out.count("The answer is 4.") == 1


def test_echo_reasoning_shown_in_verbose():
    r, _, buf = _renderer()
    r.verbose = True
    r.on_turn_start("review the repo")
    r.on_event(normalize(_MSG_STEP))
    out = buf.getvalue()
    # Verbose reveals the echo reasoning, memory, and the send_message tool line.
    assert "Executed:" in out
    assert "send_message" in out
    assert "memory" in out


def test_one_shot_completion_does_not_duplicate_bubbled_result():
    """One-shot: a final_result already shown as a bubble isn't repeated in panel."""
    r, _, buf = _renderer(one_shot=True)
    r.on_turn_start("review")
    r.on_event(normalize(_MSG_STEP))
    # SessionDone whose final_result equals the bubble text.
    r.on_event(normalize({
        "type": "session_completion",
        "data": {"success": True, "total_steps": 1,
                 "metrics": {"final_result": _LONG_MD}},
    }))
    out = buf.getvalue()
    # "Repository Review" appears once (the bubble), not twice.
    assert out.count("Repository Review") == 1


# ---------------------------------------------------------------------------
# Three-layer composition — default view, summary line, /steps trace
# ---------------------------------------------------------------------------


_TOOL_STEP = {
    "type": "step",
    "step": 1,
    "data": {
        "actions": [
            {"action_type": "read_file", "name": "read_file", "service": "fs",
             "params": {"file_path": "README.md"}}
        ],
        "agent_name": "executor",
        "reasoning": "I will read the README first.",
        "context": {"outputs": {"memory": "reading"},
                    "metrics": {"token_count": 1000}},
    },
    "agent_name": "executor",
}


def test_default_view_hides_nontool_trace_events():
    """Non-tool trace events stay hidden in the default view.

    (Tool calls/results are now shown by default — see test_tool_transparency —
    so they are NOT in this list; everything else here is still silent.)
    """
    r, state, buf = _renderer()
    for raw in (
        {"type": "session_start", "data": {"task": "t", "model_name": "m"}},
        {"type": "available_actions", "data": {"total_actions": 22}},
        {"type": "llm_request", "data": {"model_name": "m", "token_count": 10}},
        {"type": "iteration_complete",
         "data": {"iteration": 1, "iteration_status": "completed"}},
        {"type": "agent_end", "data": {"agent_id": "a", "steps": 1}},
        {"type": "session_completion",
         "data": {"success": True, "total_steps": 1, "metrics": {}}},
    ):
        ev = normalize(raw)
        state.update(ev)
        r.on_event(ev)
    assert buf.getvalue() == ""


def test_turn_summary_line_after_non_trivial_turn():
    r, state, buf = _renderer()
    r.on_turn_start("review the repo")
    for raw in (_TOOL_STEP, _MSG_STEP | {"step": 2}):
        ev = normalize(raw)
        state.update(ev)
        r.on_event(ev)
    r.on_turn_end(_PLUMBING)
    out = buf.getvalue()
    assert "2 steps" in out
    assert "1 tool" in out


def test_no_summary_line_after_trivial_turn():
    r, state, buf = _renderer()
    r.on_turn_start("hi")
    ev = normalize(_MSG_STEP)
    state.update(ev)
    r.on_event(ev)
    r.on_turn_end(_PLUMBING)
    out = buf.getvalue()
    assert "steps" not in out
    assert "tool" not in out


def test_agent_message_has_speaker_mark():
    """The dialog identity: a ● rob speaker line above the markdown body."""
    r, _, buf = _renderer()
    r.on_turn_start("hi")
    r.on_event(normalize(_MSG_STEP))
    out = buf.getvalue()
    assert "● rob" in out


def test_render_trace_replays_last_turn():
    """/steps: the buffered turn replays as full trace, including step blocks."""
    r, state, buf = _renderer()
    r.on_turn_start("review")
    for raw in (_TOOL_STEP,
                {"type": "tool_execution", "step": 1,
                 "data": {"tool_name": "fs", "action_name": "read_file",
                          "success": True, "duration_seconds": 0.1,
                          "result_preview": "readme",
                          "parameters": {"file_path": "README.md"}}},
                {"type": "iteration_complete",
                 "data": {"iteration": 1, "iteration_status": "completed"}}):
        ev = normalize(raw)
        state.update(ev)
        r.on_event(ev)
    # The tool-call line shows live by default now (from the tool_execution event,
    # paired → then ✓); the iteration marker stays trace-hidden.
    live = buf.getvalue()
    assert "read_file" in live
    assert "iter 1" not in live
    count = r.render_trace()
    assert count == 3
    out = buf.getvalue()
    assert "Step 1" in out
    assert "read_file" in out
    assert "iter 1" in out


def test_verbose_renders_step_blocks_live():
    r, state, buf = _renderer()
    r.verbose = True
    ev = normalize(_TOOL_STEP)
    state.update(ev)
    r.on_event(ev)
    out = buf.getvalue()
    assert "Step 1" in out
    assert "read_file" in out
    assert "I will read the README first." in out


def test_one_shot_preamble_bubble_does_not_suppress_final_answer():
    """polyrob run: a progress bubble followed by a done() answer renders BOTH."""
    r, state, buf = _renderer(one_shot=True)
    r.on_turn_start("read README and summarize")
    preamble = normalize({
        "type": "step", "step": 1,
        "data": {"actions": [
            {"action_type": "send_message", "name": "message", "service": "send",
             "params": {"text": "Let me start by reading the README.",
                        "wait_for_response": False}}],
            "agent_name": "executor",
            "context": {"outputs": {"memory": ""}}},
    })
    state.update(preamble)
    r.on_event(preamble)
    r.on_turn_end("This project is an autonomous AI agent platform.")
    out = buf.getvalue()
    assert "Let me start by reading the README." in out
    assert "This project is an autonomous AI agent platform." in out


def test_repl_bubble_still_suppresses_done_recap():
    r, state, buf = _renderer(one_shot=False)
    r.on_turn_start("q")
    ev = normalize(_MSG_STEP)
    state.update(ev)
    r.on_event(ev)
    r.on_turn_end("A recap that differs from the bubble text.")
    assert "A recap that differs" not in buf.getvalue()


def test_default_console_resolves_stdout_dynamically():
    """Regression: the default Console must use file=None so Rich resolves
    sys.stdout on each write — load-bearing for the persistent app, which runs
    under patch_stdout() (capturing stdout at construction bypassed the proxy and
    corrupted the pinned box: duplicated frame, text inside the border)."""
    from cli.ui.rich_renderer import RichRenderer
    from cli.ui.state import SessionState

    r = RichRenderer(SessionState())  # no console injected → default construction
    assert r.console._file is None
