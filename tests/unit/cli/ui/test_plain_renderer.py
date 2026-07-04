"""Golden-output tests for cli.ui.plain_renderer.PlainRenderer.

All output goes to a StringIO so tests are fully deterministic without
touching stdout/stderr.  Tests verify the three-layer composition:

1. DEFAULT VIEW = dialog only: `› ` user echo, `rob:` message blocks,
   `error:` lines, plus one `(…)` activity summary per non-trivial turn.
   No bracketed trace lines, ever.
2. TRACE (verbose=True or render_trace): the bracketed event lines, with the
   real field names (regression guard for wrong-key bugs) and no truncation.
3. Turn lifecycle: answer printed exactly once; plumbing suppressed.
"""

import io
import pytest

from cli.ui.events import (
    AgentEnd,
    AgentRegistration,
    ErrorEvent,
    Info,
    IterationDone,
    LLMCall,
    SessionDone,
    SessionStart,
    Step,
    ToolExec,
)
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _renderer(*, verbose: bool = False) -> tuple[PlainRenderer, io.StringIO, SessionState]:
    buf = io.StringIO()
    state = SessionState()
    rend = PlainRenderer(state=state, stream=buf)
    rend.verbose = verbose
    return rend, buf, state


def _lines(buf: io.StringIO) -> list[str]:
    return [ln for ln in buf.getvalue().splitlines() if ln]


# ---------------------------------------------------------------------------
# DEFAULT VIEW — dialog layer only (the chat recomposition contract)
# ---------------------------------------------------------------------------


def test_default_hides_nontool_trace_events():
    """The default view renders NOTHING for non-tool trace-layer events.

    (Tool calls/results are shown by default now — see test_tool_transparency —
    so ToolExec is not in this list; everything else here is still silent.)
    """
    rend, buf, state = _renderer()
    events = [
        SessionStart(model_name="m", task="t"),
        LLMCall(model_name="m", token_count=100),
        IterationDone(iteration=1, iteration_status="completed"),
        Info(type="available_actions", content=""),
        AgentRegistration(agent_id="a", agent_name="executor"),
        AgentEnd(agent_id="a", steps=1),
        SessionDone(success=True, total_steps=1, final_result="ok"),
    ]
    for ev in events:
        state.update(ev)
        rend.on_event(ev)
    assert buf.getvalue() == ""


def test_default_step_renders_message_and_tool_calls():
    """A step's message is dialog AND its real tool calls show by default.

    The post-`[step]` scaffolding (header, reasoning, memory) is still trace
    (verbose-only); send_message is the bubble, not a tool line.
    """
    rend, buf, state = _renderer()
    ev = Step(
        step=3,
        reasoning="I will reply",
        memory="some memory",
        actions=[
            {"action_type": "read_file", "name": "read_file", "service": "fs",
             "params": {"file_path": "x"}},
            {"action_type": "send_message", "name": "message", "service": "send",
             "params": {"text": "hello there"}},
        ],
    )
    state.update(ev)
    rend.on_event(ev)
    # The read_file tool line now comes from the tool_execution event (→ then ✓),
    # not the terminal Step — a real run always emits this.
    xev = ToolExec(tool_name="fs", action_name="read_file", success=True,
                   duration_seconds=0.1, result_preview="ok", parameters={"file_path": "x"})
    state.update(xev)
    rend.on_event(xev)
    out = buf.getvalue()
    assert "rob:" in out
    assert "hello there" in out
    # The read_file tool call shows by default (as a → call line)...
    assert "read_file" in out
    assert "→" in out
    # ...but send_message is the bubble, not a tool line, and scaffolding/reasoning stay trace.
    assert "→ send_message" not in out
    assert "[step" not in out
    assert "I will reply" not in out


def test_default_error_event_renders_unbracketed():
    rend, buf, state = _renderer()
    ev = ErrorEvent(error_type="ToolError", error_message="something broke")
    state.update(ev)
    rend.on_event(ev)
    out = buf.getvalue()
    assert "error: ToolError: something broke" in out
    assert "[error]" not in out


def test_default_failed_session_surfaces_error_message():
    """A failed session must explain itself — never a bare FAILED steps=0."""
    rend, buf, state = _renderer()
    ev = SessionDone(
        success=False, total_steps=0,
        error_message="FATAL ERROR: Primary LLM failed: quota exceeded",
    )
    state.update(ev)
    rend.on_event(ev)
    out = buf.getvalue()
    assert "error: session failed: FATAL ERROR" in out
    assert "[done]" not in out


def test_turn_summary_line_for_non_trivial_turn():
    """A turn with tool work leaves one (…) activity summary line."""
    rend, buf, state = _renderer()
    rend.on_turn_start("do work")
    for n in (1, 2):
        ev = Step(step=n, actions=[
            {"action_type": "read_file", "name": "read_file", "service": "fs",
             "params": {"file_path": "x"}},
        ])
        state.update(ev)
        rend.on_event(ev)
    rend.on_turn_end("done with the work")
    out = buf.getvalue()
    summary = [ln for ln in out.splitlines() if ln.startswith("(")]
    assert len(summary) == 1
    assert "2 steps" in summary[0]
    assert "2 tools" in summary[0]


_FENCED_BRAIN = (
    "```json\n"
    "{\n"
    '  "current_state": {\n'
    '    "evaluation_previous_goal": "Success - greeted user",\n'
    '    "memory": "User greeted me.",\n'
    '    "next_goal": "Reply to greeting and offer assistance",\n'
    '    "reasoning": "Simple greeting -> respond warmly.",\n'
    '    "phase": "discovery"\n'
    "  }\n"
    "}\n"
    "```"
)


def test_brain_state_answer_demoted_not_dumped():
    """OR-2: a tool-free turn whose final answer is (fenced) brain-state JSON must
    NOT be dumped raw as a 'rob:' bubble. The plain renderer demotes it to an
    honest 'finished without a final message' notice, mirroring the rich renderer.
    """
    rend, buf, state = _renderer()
    rend._one_shot = True
    rend.on_turn_start("Hey, how's it going?")
    rend.on_turn_end(_FENCED_BRAIN)
    out = buf.getvalue()
    # The raw brain-state JSON must never reach the user.
    assert "current_state" not in out
    assert "```json" not in out
    assert "evaluation_previous_goal" not in out
    # The absence is made explicit, with the planning goal as context.
    assert "finished without a final message" in out
    assert "Reply to greeting and offer assistance" in out


def test_one_shot_suppresses_done_recap_after_send_message():
    """OR-1: in one-shot, a send_message reply + a done() recap must render only
    ONE bubble (the reply). The bookkeeping recap is suppressed."""
    rend, buf, state = _renderer()
    rend._one_shot = True
    rend.on_turn_start("Hey, how's it going?")
    ev = Step(step=1, actions=[
        {"action_type": "send_message", "name": "message", "service": "send",
         "params": {"text": "Hey! Doing great, thanks for asking. How can I help?"}},
    ])
    state.update(ev)
    rend.on_event(ev)
    rend.on_turn_end("Responded to the user's greeting. No further action needed.")
    out = buf.getvalue()
    assert out.count("rob:") == 1
    assert "Hey! Doing great" in out
    assert "No further action needed" not in out


def test_one_shot_keeps_new_final_answer_after_send_message():
    """OR-1 guard: a genuine NEW final answer after a progress note still renders."""
    rend, buf, state = _renderer()
    rend._one_shot = True
    rend.on_turn_start("What's 2+2?")
    ev = Step(step=1, actions=[
        {"action_type": "send_message", "name": "message", "service": "send",
         "params": {"text": "Let me compute that."}},
    ])
    state.update(ev)
    rend.on_event(ev)
    rend.on_turn_end("The answer is 4.")
    out = buf.getvalue()
    assert "The answer is 4." in out


def test_no_summary_line_for_trivial_turn():
    """A plain chat reply (one message step, no tools) leaves no summary."""
    rend, buf, state = _renderer()
    rend.on_turn_start("hi")
    ev = Step(step=1, actions=[
        {"action_type": "send_message", "name": "message", "service": "send",
         "params": {"text": "hello!"}},
    ])
    state.update(ev)
    rend.on_event(ev)
    rend.on_turn_end("Message sent to user (non-blocking)")
    out = buf.getvalue()
    assert not [ln for ln in out.splitlines() if ln.startswith("(")]


def test_steps_replay_renders_trace_from_buffer():
    """render_trace() replays the buffered turn as bracketed trace lines."""
    rend, buf, state = _renderer()
    rend.on_turn_start("do work")
    ev = Step(step=1, reasoning="thinking hard", actions=[
        {"action_type": "read_file", "name": "read_file", "service": "fs",
         "params": {"file_path": "x"}},
    ])
    state.update(ev)
    rend.on_event(ev)
    assert "[step" not in buf.getvalue()  # hidden live
    replayed = rend.render_trace()
    assert replayed == 1
    out = buf.getvalue()
    assert "[step 1] reasoning: thinking hard" in out
    assert "→ read_file" in out


def test_steps_replay_empty_buffer_returns_zero():
    rend, buf, state = _renderer()
    assert rend.render_trace() == 0


# ---------------------------------------------------------------------------
# TRACE LAYER (verbose) — line formats are the pre-recomposition contract
# ---------------------------------------------------------------------------


def test_session_start_prefix_and_model():
    rend, buf, state = _renderer(verbose=True)
    ev = SessionStart(model_name="gemini-2.5-flash", task="do X")
    state.update(ev)
    rend.on_event(ev)
    lines = _lines(buf)
    assert len(lines) == 1
    assert lines[0].startswith("[session]")
    assert "gemini-2.5-flash" in lines[0]


def test_session_start_task_quoted():
    rend, buf, state = _renderer(verbose=True)
    ev = SessionStart(model_name="m", task="scrape the news")
    state.update(ev)
    rend.on_event(ev)
    assert "scrape the news" in buf.getvalue()


def test_session_start_vision_flag():
    rend, buf, state = _renderer(verbose=True)
    ev = SessionStart(model_name="m", use_vision=True)
    state.update(ev)
    rend.on_event(ev)
    assert "vision=yes" in buf.getvalue()


def test_llm_call_shows_cost_estimate():
    """cost_estimate must appear in the output, never the wrong 'cost' key."""
    rend, buf, state = _renderer(verbose=True)
    ev = LLMCall(
        model_name="gpt-4o",
        provider="openai",
        prompt_tokens=1024,
        completion_tokens=256,
        token_count=1280,
        cost_estimate=0.001234,
        duration_seconds=1.5,
        success=True,
    )
    state.update(ev)
    rend.on_event(ev)
    out = buf.getvalue()
    assert "[llm]" in out
    assert "gpt-4o" in out
    assert "cost=$0.001234" in out


def test_llm_call_shows_token_fields():
    """prompt_tokens / completion_tokens / token_count must appear (not total_tokens)."""
    rend, buf, state = _renderer(verbose=True)
    ev = LLMCall(
        model_name="m",
        prompt_tokens=500,
        completion_tokens=100,
        token_count=600,
    )
    state.update(ev)
    rend.on_event(ev)
    out = buf.getvalue()
    assert "in=500" in out
    assert "out=100" in out
    assert "total=600" in out


def test_llm_call_none_tokens_not_shown():
    """When token fields are None they must not appear as 'None' in the output."""
    rend, buf, state = _renderer(verbose=True)
    ev = LLMCall(model_name="m", prompt_tokens=None, completion_tokens=None,
                 token_count=None, cost_estimate=None)
    state.update(ev)
    rend.on_event(ev)
    out = buf.getvalue()
    # None values must be absent — not rendered as "None"
    assert "None" not in out
    assert "in=" not in out
    assert "out=" not in out
    assert "cost=" not in out


def test_llm_call_fail_status():
    rend, buf, state = _renderer(verbose=True)
    ev = LLMCall(model_name="m", success=False)
    state.update(ev)
    rend.on_event(ev)
    assert "status=FAIL" in buf.getvalue()


def test_llm_call_success_status():
    rend, buf, state = _renderer(verbose=True)
    ev = LLMCall(model_name="m", success=True)
    state.update(ev)
    rend.on_event(ev)
    assert "status=ok" in buf.getvalue()


def test_step_prefix_includes_step_number():
    rend, buf, state = _renderer(verbose=True)
    ev = Step(step=7, reasoning="look at config", memory="", actions=[])
    state.update(ev)
    rend.on_event(ev)
    assert "[step 7]" in buf.getvalue()


def test_step_reasoning_printed_in_full():
    """No hard truncation — long reasoning must appear fully."""
    long_reasoning = "x" * 500  # well over the old 200-char limit
    rend, buf, state = _renderer(verbose=True)
    ev = Step(step=1, reasoning=long_reasoning, memory="", actions=[])
    state.update(ev)
    rend.on_event(ev)
    assert long_reasoning in buf.getvalue()


def test_step_memory_shown():
    rend, buf, state = _renderer(verbose=True)
    ev = Step(step=2, reasoning="", memory="tracking auth refactor", actions=[])
    state.update(ev)
    rend.on_event(ev)
    assert "tracking auth refactor" in buf.getvalue()


def test_step_actions_shown():
    rend, buf, state = _renderer(verbose=True)
    actions = [{"action_type": "read_file", "name": "read_file", "service": "fs",
                "params": {"file_path": "config.py"}}]
    ev = Step(step=3, reasoning="", memory="", actions=actions)
    state.update(ev)
    rend.on_event(ev)
    out = buf.getvalue()
    assert "→ read_file" in out


def test_step_empty_reasoning_no_line():
    """When reasoning is empty, no reasoning line is emitted."""
    rend, buf, state = _renderer(verbose=True)
    ev = Step(step=1, reasoning="", memory="", actions=[])
    state.update(ev)
    rend.on_event(ev)
    assert "reasoning" not in buf.getvalue()


def test_tool_exec_prefix():
    rend, buf, state = _renderer(verbose=True)
    ev = ToolExec(tool_name="filesystem", action_name="read_file", success=True,
                  duration_seconds=0.1)
    state.update(ev)
    rend.on_event(ev)
    out = buf.getvalue()
    assert "[tool]" in out
    assert "filesystem/read_file" in out
    assert "status=ok" in out


def test_tool_exec_fail_with_error():
    rend, buf, state = _renderer(verbose=True)
    ev = ToolExec(tool_name="browser", action_name="navigate_to", success=False,
                  error="timeout")
    state.update(ev)
    rend.on_event(ev)
    out = buf.getvalue()
    assert "status=fail" in out
    assert "timeout" in out


def test_iter_done_prefix():
    rend, buf, state = _renderer(verbose=True)
    ev = IterationDone(iteration=5, iteration_status="completed", is_done=False)
    state.update(ev)
    rend.on_event(ev)
    out = buf.getvalue()
    assert "[iter 5]" in out
    assert "completed" in out


def test_iter_done_is_done_flag():
    rend, buf, state = _renderer(verbose=True)
    ev = IterationDone(iteration=10, iteration_status="done", is_done=True)
    state.update(ev)
    rend.on_event(ev)
    assert "DONE" in buf.getvalue()


def test_error_prefix_in_trace_replay():
    """The trace layer keeps the [error] line format (via render_trace)."""
    rend, buf, state = _renderer()
    rend.on_turn_start("t")
    ev = ErrorEvent(error_type="ToolError", error_message="something broke")
    state.update(ev)
    rend.on_event(ev)
    rend.render_trace()
    out = buf.getvalue()
    assert "[error]" in out
    assert "ToolError" in out
    assert "something broke" in out


def test_session_done_completed():
    """[done] (verbose) prints summary (status + steps) but NOT the result text.

    The answer text is rendered exactly once via on_turn_end(), never embedded
    in the [done] line (single-render guard).
    """
    rend, buf, state = _renderer(verbose=True)
    ev = SessionDone(success=True, total_steps=8, final_result="All done.")
    state.update(ev)
    rend.on_event(ev)
    out = buf.getvalue()
    assert "[done]" in out
    assert "completed" in out
    assert "steps=8" in out
    # Result text must NOT appear in the [done] line — it comes from on_turn_end.
    assert "All done." not in out


def test_session_done_failed():
    rend, buf, state = _renderer(verbose=True)
    ev = SessionDone(success=False, total_steps=3)
    state.update(ev)
    rend.on_event(ev)
    assert "FAILED" in buf.getvalue()


def test_info_event_printed():
    rend, buf, state = _renderer(verbose=True)
    ev = Info(type="status", content="")
    state.update(ev)
    rend.on_event(ev)
    assert "[info]" in buf.getvalue()
    assert "status" in buf.getvalue()


def test_unknown_event_does_not_crash():
    """Any event object, even an unsupported subtype, must not crash."""
    rend, buf, state = _renderer()
    ev = Info(type="some_future_type", content="payload")
    # on_event must not raise
    rend.on_event(ev)


# ---------------------------------------------------------------------------
# turn lifecycle
# ---------------------------------------------------------------------------


def test_on_turn_start_writes_user_echo():
    rend, buf, state = _renderer()
    rend.on_turn_start("hello agent")
    out = buf.getvalue()
    assert "› hello agent" in out
    assert "[turn]" not in out


def test_on_turn_end_writes_answer():
    rend, buf, state = _renderer()
    rend.on_turn_end("The answer is 42.")
    out = buf.getvalue()
    assert "rob:" in out
    assert "The answer is 42." in out


def test_on_stream_delta_buffered_and_flushed_on_turn_end():
    rend, buf, state = _renderer()
    rend.on_stream_delta("Hello ")
    rend.on_stream_delta("world")
    # nothing flushed yet
    assert "rob:" not in buf.getvalue()
    rend.on_turn_end("")  # empty answer → use buffered
    assert "Hello world" in buf.getvalue()


def test_on_stream_delta_cleared_on_turn_start():
    rend, buf, state = _renderer()
    rend.on_stream_delta("stale")
    rend.on_turn_start("new turn")
    rend.on_turn_end("reply")
    # "stale" must NOT appear anywhere after the reset
    out = buf.getvalue()
    assert "stale" not in out
    assert "reply" in out


# ---------------------------------------------------------------------------
# render_status
# ---------------------------------------------------------------------------


def test_render_status_shows_status():
    rend, buf, state = _renderer()
    state.status = "running"
    state.step = 3
    rend.render_status()
    out = buf.getvalue()
    assert "[status]" in out
    assert "running" in out
    assert "step=3" in out


def test_render_status_shows_cost_when_nonzero():
    rend, buf, state = _renderer()
    state.cost_estimate_total = 0.0056
    rend.render_status()
    assert "cost=$0.005600" in buf.getvalue()


def test_render_status_shows_ctx_when_nonzero():
    rend, buf, state = _renderer()
    state.ctx_percent = 55.0
    rend.render_status()
    assert "ctx=55%" in buf.getvalue()


# ---------------------------------------------------------------------------
# print_block
# ---------------------------------------------------------------------------


def test_print_block_no_title():
    rend, buf, state = _renderer()
    rend.print_block("Some output text")
    assert "Some output text" in buf.getvalue()


def test_print_block_with_title():
    rend, buf, state = _renderer()
    rend.print_block("content here", title="My Section")
    out = buf.getvalue()
    assert "My Section" in out
    assert "content here" in out


# ---------------------------------------------------------------------------
# Dialog-first parity — message bubble + plumbing suppression + demotion
# ---------------------------------------------------------------------------


_LONG_MD = "## Repository Review\n\nfull body line one\nfull body line two\n"

_MSG_STEP = Step(
    step=1,
    reasoning="Executed: send_message(text=## Repository Review, "
    "wait_for_response=True)",
    memory="send_message(text=##)→DONE",
    actions=[{"action_type": "send_message", "name": "message", "service": "send",
              "params": {"text": _LONG_MD, "wait_for_response": True}}],
)

_PLUMBING = "Message sent to user. Task paused - will resume when user responds."


def test_plain_message_step_renders_bubble_no_tool_line():
    rend, buf, state = _renderer()
    rend.on_turn_start("review the repo")
    state.update(_MSG_STEP)
    rend.on_event(_MSG_STEP)
    out = buf.getvalue()
    assert "rob:" in out
    assert "Repository Review" in out          # full text
    assert "full body line two" in out          # multi-line preserved
    assert "→ send_message" not in out          # tool line is trace
    assert "Executed:" not in out               # echo reasoning is trace
    assert "[step 1]" not in out                # scaffolding is trace


def test_plain_turn_end_suppresses_plumbing_after_bubble():
    rend, buf, state = _renderer()
    rend.on_turn_start("review the repo")
    rend.on_event(_MSG_STEP)
    rend.on_turn_end(_PLUMBING)
    out = buf.getvalue()
    assert "Task paused" not in out


def test_plain_turn_end_suppresses_plumbing_without_bubble():
    rend, buf, state = _renderer()
    rend.on_turn_start("hi")
    rend.on_turn_end(_PLUMBING)
    assert "Task paused" not in buf.getvalue()


def test_plain_turn_end_renders_real_answer_when_no_bubble():
    rend, buf, state = _renderer()
    rend.on_turn_start("what is 2+2?")
    rend.on_turn_end("The answer is 4.")
    out = buf.getvalue()
    assert out.count("The answer is 4.") == 1


def test_plain_echo_reasoning_and_memory_shown_in_verbose():
    rend, buf, state = _renderer(verbose=True)
    rend.on_turn_start("review the repo")
    rend.on_event(_MSG_STEP)
    out = buf.getvalue()
    assert "Executed:" in out
    assert "send_message" in out
    assert "memory:" in out


def test_plain_mixed_step_default_renders_bubble_and_tool_call():
    """Mixed steps (tools + message): the message is the bubble AND the real tool
    call shows by default; the [step] scaffolding/reasoning stay trace."""
    rend, buf, state = _renderer()
    rend.on_turn_start("do work")
    mixed = Step(
        step=2,
        reasoning="I will add a todo and notify the user.",
        memory="",
        actions=[
            {"action_type": "task_todo_add", "name": "task_todo_add",
             "service": "task", "params": {"content": "do it"}},
            {"action_type": "send_message", "name": "message", "service": "send",
             "params": {"text": "working on it"}},
        ],
    )
    state.update(mixed)
    rend.on_event(mixed)
    # The tool line comes from the tool_execution event (→ then ✓).
    xev = ToolExec(tool_name="task", action_name="task_todo_add", success=True,
                   duration_seconds=0.0, parameters={"content": "do it"})
    state.update(xev)
    rend.on_event(xev)
    out = buf.getvalue()
    assert "rob:" in out
    assert "working on it" in out
    assert "task_todo_add" in out            # the tool call shows by default
    assert "[step 2]" not in out             # scaffolding is still trace
    assert "I will add a todo" not in out     # reasoning is still trace
    # ... and the trace replay reveals the full scaffolding:
    rend.render_trace()
    out = buf.getvalue()
    assert "[step 2]" in out
    assert "→ task_todo_add" in out
    assert "I will add a todo" in out


def test_plain_one_shot_message_step_is_dialog_only():
    """One-shot (polyrob run) now follows the same layer rules as the REPL:
    scaffolding is trace (verbose-only); the message is the dialog."""
    buf = io.StringIO()
    state = SessionState()
    rend = PlainRenderer(state=state, stream=buf, one_shot=True)
    msg_step = Step(
        step=1,
        reasoning="I will summarize the repo and message the user.",
        memory="",
        actions=[{"action_type": "send_message", "name": "message",
                  "service": "send", "params": {"text": _LONG_MD}}],
    )
    state.update(msg_step)
    rend.on_event(msg_step)
    out = buf.getvalue()
    assert "[step 1]" not in out
    assert "I will summarize" not in out
    assert "rob:" in out
    assert "Repository Review" in out


# ---------------------------------------------------------------------------
# Scripted sequences — golden output
# ---------------------------------------------------------------------------


def test_golden_sequence_default_is_pure_dialog():
    """The default view of a scripted run: user echo, message, summary. Nothing else."""
    rend, buf, state = _renderer()
    rend.on_turn_start("count to 3")
    events = [
        SessionStart(model_name="gemini-2.5-flash", task="count to 3"),
        LLMCall(model_name="gemini-2.5-flash", provider="gemini",
                prompt_tokens=100, completion_tokens=20, token_count=120,
                cost_estimate=0.000048, duration_seconds=0.8, success=True),
        Step(step=1, reasoning="I will count: 1, 2, 3", memory="counting",
             actions=[{"action_type": "read_file", "name": "read_file",
                       "service": "fs", "params": {"file_path": "x"}}]),
        # A real run emits the tool_execution — it carries the call line (→) + result (✓).
        ToolExec(tool_name="fs", action_name="read_file", success=True,
                 duration_seconds=0.1, result_preview="ok",
                 parameters={"file_path": "x"}),
        Step(step=2, reasoning="", memory="",
             actions=[{"action_type": "send_message", "name": "message",
                       "service": "send", "params": {"text": "1, 2, 3"}}]),
        ToolExec(tool_name="task", action_name="done", success=True,
                 duration_seconds=0.01),
        SessionDone(success=True, total_steps=2, final_result="1, 2, 3"),
    ]
    for ev in events:
        state.update(ev)
        rend.on_event(ev)
    rend.on_turn_end("1, 2, 3")

    lines = _lines(buf)
    assert lines[0] == "› count to 3"
    # read_file's call (→) then result (✓) — paired, correct order; the done tool
    # exec is the dialog channel and is suppressed.
    assert lines[1].lstrip().startswith("→")
    assert "read_file" in lines[1]
    assert lines[2].lstrip().startswith("✓")
    assert "read_file" in lines[2]
    assert lines[3] == "rob:"
    assert lines[4] == "  1, 2, 3"
    assert lines[5].startswith("(")           # the activity summary
    assert "2 steps" in lines[5]
    assert len(lines) == 6
    # Still no bracketed trace scaffolding in the default view.
    assert not any(ln.startswith("[") for ln in lines)


def test_golden_sequence_verbose_trace():
    """Verbose keeps the stable bracketed trace with the real field names."""
    rend, buf, state = _renderer(verbose=True)

    events = [
        SessionStart(model_name="gemini-2.5-flash", task="count to 3"),
        LLMCall(model_name="gemini-2.5-flash", provider="gemini",
                prompt_tokens=100, completion_tokens=20, token_count=120,
                cost_estimate=0.000048, duration_seconds=0.8, success=True),
        Step(step=1, reasoning="I will count: 1, 2, 3", memory="counting",
             actions=[{"name": "done", "params": {}}]),
        ToolExec(tool_name="task", action_name="done", success=True,
                 duration_seconds=0.01),
        SessionDone(success=True, total_steps=1, final_result="1, 2, 3"),
    ]

    for ev in events:
        state.update(ev)
        rend.on_event(ev)

    out = buf.getvalue()
    lines = [ln for ln in out.splitlines() if ln]

    # Check order via prefixes
    prefixes = [ln.split("]")[0].lstrip("[") for ln in lines]
    assert "session" in prefixes[0]
    assert "llm" in prefixes[1]
    assert "step 1" in prefixes[2]  # reasoning line
    assert any("tool" in p for p in prefixes)
    assert any("done" in p for p in prefixes)

    # Spot-check content
    assert "gemini-2.5-flash" in out
    assert "cost=$0.000048" in out
    assert "I will count: 1, 2, 3" in out
    # final_result text must NOT appear in the [done] line (single-render guard).
    done_lines = [ln for ln in out.splitlines() if ln.startswith("[done]")]
    assert len(done_lines) == 1
    assert "1, 2, 3" not in done_lines[0]
    assert "result=" not in done_lines[0]


# ---------------------------------------------------------------------------
# Single-render guard — run.py path
# ---------------------------------------------------------------------------


def test_run_path_answer_printed_exactly_once_via_on_turn_end():
    """Simulate the rob-run path: feed emits SessionDone, then on_turn_end is
    called with the result.  The answer must appear exactly once (in the
    rob: block), never duplicated by the SessionDone event.
    """
    rend, buf, state = _renderer()
    answer = "The capital of France is Paris."
    rend.on_turn_start("capital of France?")

    # 1. Feed event fires (mirrors _feed_callback in run.py)
    ev = SessionDone(success=True, total_steps=3, final_result=answer)
    state.update(ev)
    rend.on_event(ev)

    # 2. run.py calls on_turn_end with the session result
    rend.on_turn_end(answer)

    out = buf.getvalue()
    answer_lines = [ln for ln in out.splitlines() if answer in ln]
    assert len(answer_lines) == 1, (
        f"Expected answer to appear exactly once, found {len(answer_lines)} times: {answer_lines}"
    )
    assert "[done]" not in out


def test_session_done_summary_line_format_verbose():
    """The verbose [done] line contains status and steps but nothing else."""
    rend, buf, state = _renderer(verbose=True)
    ev = SessionDone(success=True, total_steps=5, final_result="some answer text")
    state.update(ev)
    rend.on_event(ev)
    done_line = buf.getvalue().strip()
    assert done_line == "[done] completed steps=5"


# ---------------------------------------------------------------------------
# AgentRegistration / AgentEnd — state-only, must be silent (even verbose)
# ---------------------------------------------------------------------------


def test_agent_registration_produces_no_output():
    """AgentRegistration is state-only; PlainRenderer must emit nothing."""
    rend, buf, state = _renderer(verbose=True)
    ev = AgentRegistration(
        agent_id="exec-1",
        agent_name="executor",
        agent_type="task",
        model_name="gemini-2.5-flash",
        task="do work",
    )
    state.update(ev)
    rend.on_event(ev)
    assert buf.getvalue() == "", (
        f"Expected empty output for AgentRegistration, got: {buf.getvalue()!r}"
    )


def test_agent_end_produces_no_output():
    """AgentEnd is state-only; PlainRenderer must emit nothing."""
    rend, buf, state = _renderer(verbose=True)
    ev = AgentEnd(agent_id="exec-1", steps=5, success=True)
    state.update(ev)
    rend.on_event(ev)
    assert buf.getvalue() == "", (
        f"Expected empty output for AgentEnd, got: {buf.getvalue()!r}"
    )


def test_plain_run_sequence_no_lifecycle_noise():
    """A full scripted verbose run must not contain '[event] agent_registration'
    or '[event] agent_end' lines — confirmed by a live-style sequence."""
    rend, buf, state = _renderer(verbose=True)

    events = [
        SessionStart(model_name="gemini-2.5-flash", task="say hello"),
        AgentRegistration(agent_id="exec-1", agent_name="executor",
                          agent_type="task", model_name="gemini-2.5-flash"),
        Step(step=1, reasoning="I will reply", memory="", actions=[]),
        AgentEnd(agent_id="exec-1", steps=1, success=True),
        SessionDone(success=True, total_steps=1,
                    final_result="Replied with 'hello'."),
    ]

    for ev in events:
        state.update(ev)
        rend.on_event(ev)

    out = buf.getvalue()
    assert "[event] agent_registration" not in out
    assert "[event] agent_end" not in out
    assert "[session]" in out
    assert "[done]" in out


# ---------------------------------------------------------------------------
# One-shot preamble bubble must not eat the real final answer
# ---------------------------------------------------------------------------


def test_one_shot_preamble_bubble_does_not_suppress_final_answer():
    """polyrob run: a mid-task progress message followed by a done() answer must
    render BOTH — the preamble bubble and the final answer (live-found bug)."""
    buf = io.StringIO()
    state = SessionState()
    rend = PlainRenderer(state=state, stream=buf, one_shot=True)
    rend.on_turn_start("read README and summarize")
    preamble = Step(step=1, actions=[
        {"action_type": "send_message", "name": "message", "service": "send",
         "params": {"text": "Let me start by reading the README.md file.",
                    "wait_for_response": False}},
    ])
    state.update(preamble)
    rend.on_event(preamble)
    rend.on_turn_end("This project is an autonomous AI agent platform.")
    out = buf.getvalue()
    assert "Let me start by reading" in out
    assert "This project is an autonomous AI agent platform." in out


def test_one_shot_final_answer_echoing_bubble_still_deduped():
    buf = io.StringIO()
    state = SessionState()
    rend = PlainRenderer(state=state, stream=buf, one_shot=True)
    rend.on_turn_start("q")
    msg = Step(step=1, actions=[
        {"action_type": "send_message", "name": "message", "service": "send",
         "params": {"text": "The answer is 42."}},
    ])
    state.update(msg)
    rend.on_event(msg)
    rend.on_turn_end("The answer is 42.")
    assert buf.getvalue().count("The answer is 42.") == 1


def test_repl_bubble_still_suppresses_done_recap():
    """REPL (non-one-shot): the bubble is the turn's voice; a differing
    done-recap is still suppressed (matches live REPL transcripts)."""
    rend, buf, state = _renderer()
    rend.on_turn_start("q")
    msg = Step(step=1, actions=[
        {"action_type": "send_message", "name": "message", "service": "send",
         "params": {"text": "pytest is the framework."}},
    ])
    state.update(msg)
    rend.on_event(msg)
    rend.on_turn_end("Answered your question — pytest with coverage.")
    out = buf.getvalue()
    assert "pytest is the framework." in out
    assert "Answered your question" not in out
