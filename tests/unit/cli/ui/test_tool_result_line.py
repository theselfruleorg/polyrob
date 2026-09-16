"""Tests for the CLI ``tool_result`` line (043 A16).

The typed ``tool_result`` feed event (``tools/controller/execution.py``) carries
a ``{kind, payload}`` render and, when the server computed one, a human
``narration`` line. This suite pins:

* a ``tool_result`` event normalises to a ``ToolExec`` carrying render/narration;
* ``blocks.tool_result_line`` renders ``✓ name · 0.2s · <narrated line>`` using
  the narrated line when a render is present, the scrubbed preview otherwise;
* the line stays ≤ 80 columns;
* the ``raw`` lane shows the render payload;
* a legacy ``tool_execution`` event is byte-identical to before (additive).

Renderables are driven through a width-pinned recording ``Console`` so
assertions are on plain text, deterministic across environments.
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from cli.ui import blocks
from cli.ui.events import ToolExec, normalize


def _render(renderable) -> str:
    buf = StringIO()
    console = Console(file=buf, width=80, no_color=True, highlight=False)
    console.print(renderable)
    return buf.getvalue()


def _tool_result(
    *,
    tool_name="filesystem",
    action_name="read_file",
    success=True,
    duration_seconds=0.2,
    error=None,
    result_preview="file content...",
    result_truncated=False,
    render=None,
    narration=None,
    extra_data=None,
    step=3,
) -> dict:
    """Mirror ``SessionManager.add_to_feed('tool_result', …)`` output shape."""
    data = {
        "tool_name": tool_name,
        "action_name": action_name,
        "success": success,
        "duration_seconds": duration_seconds,
        "error": error,
        "result_size": len(result_preview or ""),
        "result_truncated": result_truncated,
        "result_preview": result_preview,
        "call_id": "call-1",
        "render": render if render is not None else {"kind": "text", "payload": {"text": result_preview or ""}},
        "artifact_id": None,
        "step": step,
    }
    if narration is not None:
        data["narration"] = narration
    if extra_data:
        data.update(extra_data)
    return {"type": "tool_result", "timestamp": 1234567890.0, "data": data}


# --------------------------------------------------------------------- normalize


def test_tool_result_normalizes_to_toolexec_with_render_and_narration():
    ev = normalize(_tool_result(narration="Read one file in ~/price-watch"))
    assert isinstance(ev, ToolExec)
    assert ev.tool_name == "filesystem"
    assert ev.action_name == "read_file"
    assert ev.success is True
    assert ev.step == 3
    assert ev.render == {"kind": "text", "payload": {"text": "file content..."}}
    assert ev.narration == "Read one file in ~/price-watch"


def test_tool_execution_still_normalizes_without_render_or_narration():
    """Additive: a legacy tool_execution event carries no render/narration."""
    raw = {
        "type": "tool_execution",
        "step": 4,
        "data": {
            "tool_name": "browser",
            "action_name": "navigate_to",
            "success": True,
            "duration_seconds": 0.5,
            "result_preview": "ok",
            "parameters": {"url": "https://x"},
        },
    }
    ev = normalize(raw)
    assert isinstance(ev, ToolExec)
    assert ev.render is None
    assert ev.narration is None
    assert ev.result_preview == "ok"
    assert ev.parameters == {"url": "https://x"}


# ----------------------------------------------------------------- narrated line


def test_result_line_prefers_server_narration():
    ev = normalize(_tool_result(narration="Read one file in ~/price-watch"))
    out = _render(blocks.tool_result_line(ev))
    assert "read_file" in out
    assert "0.2s" in out
    assert "Read one file in ~/price-watch" in out
    # The raw preview is NOT shown when a narration is present.
    assert "file content" not in out


def test_result_line_falls_back_to_narrate_when_no_narration():
    """No server narration + a render → the shared narrate() narrator is used."""
    ev = normalize(
        _tool_result(
            action_name="run_tests",
            render={"kind": "text", "payload": {"text": "18 passed"}},
            narration=None,
            extra_data={"passed": 18, "failed": 0},
        )
    )
    out = _render(blocks.tool_result_line(ev))
    assert "Ran the test suite and all 18 passed" in out


def test_result_line_falls_back_to_preview_without_render():
    """A legacy tool_execution ToolExec (no render) shows the scrubbed preview."""
    ev = ToolExec(
        tool_name="filesystem",
        action_name="read_file",
        success=True,
        duration_seconds=0.2,
        result_preview="the literal preview text",
        render=None,
        narration=None,
    )
    out = _render(blocks.tool_result_line(ev))
    assert "read_file" in out
    assert "the literal preview text" in out


def test_result_line_is_at_most_80_columns():
    ev = normalize(_tool_result(narration="x " * 120))
    line = blocks.tool_result_line(ev)
    assert line.cell_len <= 80


def test_result_line_raw_shows_the_payload():
    ev = normalize(
        _tool_result(
            action_name="write_file",
            render={"kind": "table", "payload": {"artifact_id": "a1", "path": "data.csv"}},
            narration="Wrote data.csv",
        )
    )
    out = _render(blocks.tool_result_line(ev, raw=True))
    assert "table" in out
    assert "data.csv" in out
    # raw shows the payload, not the narrated line.
    assert "Wrote data.csv" not in out


def test_result_line_failure_shows_error():
    ev = normalize(
        _tool_result(
            success=False,
            error="timeout after 30s",
            render={"kind": "error", "payload": {"error": "timeout after 30s"}},
        )
    )
    out = _render(blocks.tool_result_line(ev))
    assert "read_file" in out
    assert "timeout after 30s" in out


def test_render_payload_str_is_scrubbed_and_typed():
    s = blocks.render_payload_str({"kind": "file", "payload": {"path": "a.txt"}})
    assert s.startswith("file:")
    assert "a.txt" in s
