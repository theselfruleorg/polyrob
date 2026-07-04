"""Phase 5: polyrob run lifecycle glue.

Three things this guards:

1. The feed-callback contract: when ``SessionDone`` arrives, ``poll_usage`` is
   fired (reading authoritative tokens/cost off disk) BEFORE the event reaches
   the renderer — so the one-shot completion panel shows real totals.  This
   mirrors the inline ``_feed_callback`` in ``cli/commands/run.py``.
2. ``on_turn_start`` precedes ``on_turn_end`` and the one-shot RichRenderer does
   not double-render when stream chunks arrive (the box + answer guards hold).
3. When ``SessionDone`` carries a non-empty ``final_result``, ``on_turn_end``
   receives that real text instead of ``run_session``'s generic return string.
"""
import json
from io import StringIO

from rich.console import Console

from cli.ui.events import SessionDone
from cli.ui.events import normalize as normalize_event
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.rich_renderer import RichRenderer
from cli.ui.state import SessionState


class _RecordingRenderer:
    """Captures the order of lifecycle calls and the state snapshot at SessionDone."""

    def __init__(self, state):
        self._state = state
        self.calls = []
        self.tokens_at_done = None

    def on_turn_start(self, text):
        self.calls.append(("on_turn_start", text))

    def on_event(self, event):
        self.calls.append(("on_event", type(event).__name__))
        if isinstance(event, SessionDone):
            # Snapshot the totals the renderer would read for the panel.
            self.tokens_at_done = self._state.tokens_total

    def on_turn_end(self, answer):
        self.calls.append(("on_turn_end", answer))


def _make_feed_callback(state, renderer, session_dir):
    """Replicates cli/commands/run.py::_feed_callback's contract."""

    def _feed_callback(_session_id, event_dict):
        event = normalize_event(event_dict)
        state.update(event)
        if isinstance(event, SessionDone) and session_dir is not None:
            state.poll_usage(session_dir)
        renderer.on_event(event)

    return _feed_callback


def _write_usage(session_dir, *, prompt, completion, total, cost):
    usage_dir = session_dir / "data" / "llm_usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    (usage_dir / "llm_usage_0001.json").write_text(
        json.dumps(
            {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "token_count": total,
                "cost_estimate": cost,
            }
        ),
        encoding="utf-8",
    )


def test_session_done_polls_usage_before_render(tmp_path):
    state = SessionState()
    renderer = _RecordingRenderer(state)
    _write_usage(tmp_path, prompt=100, completion=50, total=150, cost=0.0012)

    cb = _make_feed_callback(state, renderer, tmp_path)
    renderer.on_turn_start("do a thing")
    # SessionDone arrives during run_session
    cb(
        "sess",
        {
            "type": "session_completion",
            "data": {"success": True, "total_steps": 3},
        },
    )
    renderer.on_turn_end("the answer")

    # poll_usage ran before the renderer saw SessionDone, so the panel totals are live.
    assert renderer.tokens_at_done == 150
    assert state.cost_estimate_total == 0.0012

    # Lifecycle ordering: start, then the SessionDone event, then end.
    names = [c[0] for c in renderer.calls]
    assert names == ["on_turn_start", "on_event", "on_turn_end"]


def test_session_done_without_usage_dir_is_safe(tmp_path):
    """No llm_usage dir → poll_usage is a no-op; render still proceeds."""
    state = SessionState()
    renderer = _RecordingRenderer(state)
    cb = _make_feed_callback(state, renderer, tmp_path)
    cb("sess", {"type": "session_completion", "data": {"success": True, "total_steps": 1}})
    assert renderer.tokens_at_done == 0  # no usage files


def test_one_shot_rich_renderer_streaming_no_double_render():
    """One-shot RichRenderer with stream chunks: answer rendered exactly once."""
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=80)
    state = SessionState()
    renderer = RichRenderer(state=state, console=console, one_shot=True)

    renderer.on_turn_start("task")
    # Chunks arrive (single-chunk astream or many — both must be safe).
    renderer.on_stream_delta("Hello ")
    renderer.on_stream_delta("world")
    renderer.on_turn_end("Hello world")

    out = buf.getvalue()
    # The answer text appears, and only once (box finalized to one static block).
    assert out.count("Hello world") == 1


def test_one_shot_completion_panel_then_on_turn_end_renders_answer_once():
    """One-shot: SessionDone panel shows final_result; on_turn_end must NOT reprint.

    Mirrors run.py's full path: SessionDone arrives during run_session (rendered
    with one_shot=True so the panel carries the answer), then run.py calls
    on_turn_end(result).  The answer must appear exactly once.
    """
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=80)
    state = SessionState()
    renderer = RichRenderer(state=state, console=console, one_shot=True)

    renderer.on_turn_start("task")
    ev = normalize_event(
        {
            "type": "session_completion",
            "data": {
                "success": True,
                "total_steps": 2,
                "metrics": {"final_result": "The answer is 42"},
            },
        }
    )
    state.update(ev)
    renderer.on_event(ev)
    renderer.on_turn_end("The answer is 42")

    out = buf.getvalue()
    assert out.count("The answer is 42") == 1


def test_one_shot_no_final_result_falls_back_to_answer_block():
    """One-shot SessionDone WITHOUT final_result → on_turn_end still prints answer."""
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=80)
    state = SessionState()
    renderer = RichRenderer(state=state, console=console, one_shot=True)

    renderer.on_turn_start("task")
    ev = normalize_event(
        {"type": "session_completion", "data": {"success": True, "total_steps": 1}}
    )
    state.update(ev)
    renderer.on_event(ev)
    renderer.on_turn_end("Recovered answer")

    out = buf.getvalue()
    assert out.count("Recovered answer") == 1


def test_one_shot_rich_renderer_no_stream_prints_answer_once():
    """One-shot RichRenderer with no stream chunks still prints the answer once."""
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=80)
    state = SessionState()
    renderer = RichRenderer(state=state, console=console, one_shot=True)

    renderer.on_turn_start("task")
    renderer.on_turn_end("Final answer")

    out = buf.getvalue()
    assert out.count("Final answer") == 1


# ---------------------------------------------------------------------------
# Item 6: final_result stashing — PlainRenderer path
# ---------------------------------------------------------------------------


def _make_stashing_feed_callback(state, renderer, final_result_cell, session_dir=None):
    """Replicates the _feed_callback + _final_result stash from cli/commands/run.py."""

    def _feed_callback(_session_id, event_dict):
        event = normalize_event(event_dict)
        state.update(event)
        if isinstance(event, SessionDone):
            if event.final_result:
                final_result_cell[:] = [event.final_result]
            if session_dir is not None:
                try:
                    state.poll_usage(session_dir)
                except Exception:
                    pass
        renderer.on_event(event)

    return _feed_callback


def test_plain_renderer_on_turn_end_receives_final_result_not_generic_string():
    """SessionDone with final_result → on_turn_end gets the real text, not the
    generic run_session return value ('Session completed successfully').

    This mirrors the cli/commands/run.py fix: _final_result stashed from the
    feed callback takes precedence over run_session's return value.
    """
    buf = StringIO()
    state = SessionState()
    renderer = PlainRenderer(state=state, stream=buf)

    final_result_cell: list[str] = []
    cb = _make_stashing_feed_callback(state, renderer, final_result_cell)

    renderer.on_turn_start("say hello")

    # Feed fires with the real agent result embedded in session_completion.
    cb(
        "sess",
        {
            "type": "session_completion",
            "data": {
                "success": True,
                "total_steps": 1,
                "metrics": {"final_result": "Replied with 'hello'."},
            },
        },
    )

    # Simulate run.py: prefer stashed final_result over generic return.
    generic_return = "Session completed successfully"
    answer = final_result_cell[0] if final_result_cell else (generic_return or "")
    renderer.on_turn_end(answer)

    out = buf.getvalue()

    # The real answer must appear exactly once, in the rob: dialog block.
    assert "rob:" in out
    assert out.count("Replied with 'hello'.") == 1

    # The generic string must NOT be rendered when final_result is set.
    assert generic_return not in out


def test_plain_renderer_suppresses_generic_completion_plumbing():
    """Dialog-first: when on_turn_end receives the generic run_session plumbing
    string ("Session completed successfully") it is SUPPRESSED — that receipt is
    not a real agent answer and must not be shown as ``[answer]``."""
    buf = StringIO()
    state = SessionState()
    renderer = PlainRenderer(state=state, stream=buf)

    final_result_cell: list[str] = []
    cb = _make_stashing_feed_callback(state, renderer, final_result_cell)

    renderer.on_turn_start("task")
    cb(
        "sess",
        {"type": "session_completion", "data": {"success": True, "total_steps": 1}},
    )

    # No final_result stashed → run.py would fall back to the generic plumbing
    # string, which the renderer now suppresses.
    fallback = "Session completed successfully"
    answer = final_result_cell[0] if final_result_cell else (fallback or "")
    renderer.on_turn_end(answer)

    out = buf.getvalue()
    answer_lines = [ln for ln in out.splitlines() if ln.startswith("[answer]")]
    assert answer_lines == []
    assert fallback not in out
