"""Tool-call transparency: tool calls + results render by default (no /verbose).

Covers both renderers. The contract (Fusion-validated, 2026-06-25; call-line source corrected 2026-07-02):
- A tool_execution prints `→ name(args)` THEN `✓ … result` (paired, correct order).
  The call line is emitted from the tool_execution event (which carries parameters +
  result), NOT the terminal Step event — that fired after execution → inverted pair.
- send_message/done are NOT double-rendered as tool lines (they are the bubble).
- /quiet (show_tools=False) mutes tool lines.
- Args + result preview are secret-scrubbed.
- Sub-agent tool lines are suppressed in the default view, shown under /verbose.
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from cli.ui.events import normalize
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.rich_renderer import RichRenderer
from cli.ui.state import SessionState


def _rich():
    buf = StringIO()
    console = Console(file=buf, width=100, no_color=True, highlight=False)
    state = SessionState()
    return RichRenderer(state, console=console), state, buf


def _plain():
    buf = StringIO()
    state = SessionState()
    return PlainRenderer(state, stream=buf), state, buf


def _tool_step(action_type="read_file", params=None, agent_name="rob"):
    return {
        "type": "step",
        "step": 1,
        "data": {
            "actions": [
                {
                    "action_type": action_type,
                    "name": action_type,
                    "service": "filesystem",
                    "params": params or {"path": "config.py"},
                }
            ],
            "agent_name": agent_name,
            "reasoning": "reading the file",
            "context": {"outputs": {"memory": ""}},
        },
        "agent_name": agent_name,
    }


def _tool_exec(tool="filesystem", action="read_file", success=True,
               duration=0.2, error=None, preview="file content here", params=None):
    return {
        "type": "tool_execution",
        "step": 1,
        "data": {
            "tool_name": tool,
            "action_name": action,
            "success": success,
            "duration_seconds": duration,
            "error": error,
            "result_preview": preview,
            "result_truncated": False,
            "parameters": params or {"path": "config.py"},
        },
    }


def _register_main(state, name="rob"):
    state.update(normalize({"type": "agent_registration",
                            "data": {"agent_id": "main_1", "agent_name": name}}))


# ---------------------------------------------------------------------------
# Default view: calls + results visible WITHOUT /verbose
# ---------------------------------------------------------------------------

def test_rich_tool_call_line_rendered_by_default():
    # The call line is now emitted from the tool_execution event (paired before ✓),
    # not the terminal Step event.
    r, state, buf = _rich()
    ev = normalize(_tool_exec())
    state.update(ev)
    r.on_event(ev)
    out = buf.getvalue()
    assert "read_file" in out
    assert "config.py" in out   # args from event.parameters
    assert "→" in out
    # correct order: the → call line precedes the ✓ result line
    assert out.index("→") < out.index("✓")


def test_step_event_does_not_emit_tool_call_line():
    # A bare Step (no tool_execution) must NOT print a → line — that would be the
    # inverted/duplicate call line the reorder removed.
    r, state, buf = _rich()
    ev = normalize(_tool_step())
    state.update(ev)
    r.on_event(ev)
    assert "→" not in buf.getvalue()


def test_rich_tool_result_line_rendered_by_default():
    r, state, buf = _rich()
    ev = normalize(_tool_exec())
    state.update(ev)
    r.on_event(ev)
    out = buf.getvalue()
    assert "file content here" in out
    assert "✓" in out


def test_plain_tool_call_and_result_rendered_by_default():
    r, state, buf = _plain()
    sev = normalize(_tool_step())
    state.update(sev)
    r.on_event(sev)
    xev = normalize(_tool_exec())
    state.update(xev)
    r.on_event(xev)
    out = buf.getvalue()
    assert "read_file" in out
    assert "file content here" in out


def test_rich_tool_failure_shows_error():
    r, state, buf = _rich()
    ev = normalize(_tool_exec(success=False, error="timeout", preview=None))
    state.update(ev)
    r.on_event(ev)
    out = buf.getvalue()
    assert "✗" in out
    assert "timeout" in out


# ---------------------------------------------------------------------------
# /quiet mutes; send_message/done are not double-rendered
# ---------------------------------------------------------------------------

def test_quiet_mutes_tool_lines_rich():
    r, state, buf = _rich()
    r.show_tools = False
    sev = normalize(_tool_step())
    state.update(sev)
    r.on_event(sev)
    xev = normalize(_tool_exec())
    state.update(xev)
    r.on_event(xev)
    out = buf.getvalue()
    assert "read_file" not in out
    assert "file content here" not in out


def test_send_message_not_rendered_as_tool_line_rich():
    r, state, buf = _rich()
    sev = normalize({
        "type": "step", "step": 1,
        "data": {"actions": [{"action_type": "send_message", "name": "message",
                              "service": "send",
                              "params": {"text": "hi there"}}],
                 "agent_name": "rob"},
        "agent_name": "rob",
    })
    state.update(sev)
    r.on_event(sev)
    out = buf.getvalue()
    assert "hi there" in out          # the bubble
    assert "→ send_message" not in out  # not a tool call line


def test_send_message_tool_exec_not_rendered_as_result_rich():
    r, state, buf = _rich()
    xev = normalize(_tool_exec(tool="send", action="send_message",
                               preview="Message sent to user (non-blocking)"))
    state.update(xev)
    r.on_event(xev)
    assert buf.getvalue() == ""


def test_done_tool_exec_not_rendered_rich():
    r, state, buf = _rich()
    xev = normalize(_tool_exec(tool="agent", action="done", preview="done"))
    state.update(xev)
    r.on_event(xev)
    assert buf.getvalue() == ""


# ---------------------------------------------------------------------------
# Secret scrub
# ---------------------------------------------------------------------------

def test_secret_scrubbed_in_call_args_rich():
    # The → call args (now sourced from the tool_execution parameters) are scrubbed.
    r, state, buf = _rich()
    ev = normalize(_tool_exec(action="set_env",
                              params={"value": "sk-abcdEFGH1234567890abcdEFGH"}))
    state.update(ev)
    r.on_event(ev)
    out = buf.getvalue()
    assert "→" in out  # the call line is present (so the scrub is actually exercised)
    assert "sk-abcdEFGH1234567890abcdEFGH" not in out


def test_secret_scrubbed_in_result_preview_rich():
    r, state, buf = _rich()
    ev = normalize(_tool_exec(action="read_file",
                              preview="API_KEY=sk-abcdEFGH1234567890abcdEFGH"))
    state.update(ev)
    r.on_event(ev)
    assert "sk-abcdEFGH1234567890abcdEFGH" not in buf.getvalue()


# ---------------------------------------------------------------------------
# Sub-agent suppression in default view
# ---------------------------------------------------------------------------

def test_subagent_tool_result_suppressed_in_default_view_rich():
    r, state, buf = _rich()
    _register_main(state, "rob")
    # A sub-agent step sets last_step_sub_agent True.
    sev = normalize(_tool_step(agent_name="leaf_9"))
    state.update(sev)
    # In default view the sub-agent's STEP start-line is already suppressed
    # (is_sub_agent), and the following tool result must be suppressed too.
    buf.truncate(0); buf.seek(0)
    xev = normalize(_tool_exec())
    state.update(xev)
    r.on_event(xev)
    assert buf.getvalue() == ""


def test_subagent_tool_result_shown_under_verbose_rich():
    r, state, buf = _rich()
    r.verbose = True
    _register_main(state, "rob")
    sev = normalize(_tool_step(agent_name="leaf_9"))
    state.update(sev)
    buf.truncate(0); buf.seek(0)
    xev = normalize(_tool_exec())
    state.update(xev)
    r.on_event(xev)
    # Verbose is a superset — the sub-agent tool result is visible.
    assert "read_file" in buf.getvalue() or "file content here" in buf.getvalue()


# ---------------------------------------------------------------------------
# verbose is a superset (does not LOSE the result preview)
# ---------------------------------------------------------------------------

def test_verbose_still_shows_tool_result_preview_rich():
    r, state, buf = _rich()
    r.verbose = True
    ev = normalize(_tool_exec(preview="important result detail"))
    state.update(ev)
    r.on_event(ev)
    assert "important result detail" in buf.getvalue()
