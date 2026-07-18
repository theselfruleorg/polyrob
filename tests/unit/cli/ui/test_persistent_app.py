"""D4/D5: the persistent bottom-anchored Application + autonomy status line.

The Application is built headlessly here (DummyOutput / pipe input) — only the
constructible pieces and the pure builders are unit-tested; the live "pins at the
bottom, content scrolls above, status updates during the turn" behaviour needs a
real TTY. The persistent input is now the DEFAULT for an interactive TTY
(opt-OUT via POLYROB_PERSISTENT_INPUT=0).
"""

from __future__ import annotations

import asyncio
import io
import re

import pytest

from cli.ui import app as cli_app
from cli.ui import statusbar
from cli.ui.state import SessionState


# ---------------------------------------------------------------------------
# Autonomy status line (second pinned line)
# ---------------------------------------------------------------------------


def test_autonomy_line_empty_without_snapshot():
    assert statusbar.autonomy_line(SessionState()) == ""


# ---------------------------------------------------------------------------
# Bug B — thin growing input (no 4-sided box)
# ---------------------------------------------------------------------------


def test_input_height_one_row_when_empty():
    """An empty buffer (0/1 logical lines) reserves exactly one row — never a
    tall box."""
    d = cli_app.input_height_dimension(0)
    assert d.min == 1
    assert d.preferred == 1


def test_input_height_grows_with_content_lines():
    """Each typed/pasted newline adds a row (the input grows with content)."""
    assert cli_app.input_height_dimension(3).preferred == 3


def test_input_height_clamps_to_max_rows():
    """Growth is capped so a huge paste can't swallow the whole terminal."""
    d = cli_app.input_height_dimension(50, max_rows=10)
    assert d.preferred == 10
    assert d.max == 10


def test_separator_label_includes_model_and_provider():
    state = SessionState()
    state.model = "glm-5.2"
    state.provider = "openrouter"
    assert cli_app.separator_label(state) == "rob · glm-5.2 · openrouter"


def test_separator_label_bare_when_no_model():
    assert cli_app.separator_label(SessionState()) == "rob"


def test_autonomy_line_renders_counts():
    s = SessionState()
    s.autonomy_snapshot = {"goals": 1, "cron": 2, "review": True}
    line = statusbar.autonomy_line(s)
    assert "goals 1" in line
    assert "cron 2" in line
    assert "review on" in line


def test_autonomy_line_omits_zero_counts():
    s = SessionState()
    s.autonomy_snapshot = {"goals": 0, "cron": 0, "review": False}
    line = statusbar.autonomy_line(s)
    # No goals/cron when zero; review off omitted.
    assert "goals" not in line
    assert "cron" not in line


# ---------------------------------------------------------------------------
# build_app — headless construction
# ---------------------------------------------------------------------------


def test_build_app_constructs_headlessly():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    submitted = []
    with create_pipe_input() as pipe:
        app, buf = cli_app.build_app(
            SessionState(),
            on_submit=lambda text: submitted.append(text),
            output=DummyOutput(),
            input=pipe,
        )
        assert app is not None
        assert buf is not None
        # The layout has at least the input + status windows.
        assert app.layout is not None


def test_build_app_accept_handler_submits_and_clears():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    submitted = []
    with create_pipe_input() as pipe:
        app, buf = cli_app.build_app(
            SessionState(),
            on_submit=lambda text: submitted.append(text),
            output=DummyOutput(),
            input=pipe,
        )
        buf.text = "hello world"
        # Simulate Enter: the accept handler fires on_submit + clears the buffer.
        buf.validate_and_handle()
        assert submitted == ["hello world"]
        assert buf.text == ""


def test_build_app_blank_submit_ignored():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    submitted = []
    with create_pipe_input() as pipe:
        app, buf = cli_app.build_app(
            SessionState(),
            on_submit=lambda text: submitted.append(text),
            output=DummyOutput(),
            input=pipe,
        )
        buf.text = "   "
        buf.validate_and_handle()
        assert submitted == []  # whitespace-only is not submitted


def test_build_app_is_not_full_screen():
    """The persistent app reserves only the bottom — native scrollback is kept
    (content scrolls above via run_in_terminal). It must NOT be full-screen."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        app, _ = cli_app.build_app(
            SessionState(),
            on_submit=lambda text: None,
            output=DummyOutput(),
            input=pipe,
        )
        assert app.full_screen is False


# ---------------------------------------------------------------------------
# Persistent key bindings (Ctrl-C interrupt, Ctrl-D exit) — headless
# ---------------------------------------------------------------------------


class _FakeBuffer:
    def __init__(self, text: str = "") -> None:
        self.text = text


class _FakeApp:
    def __init__(self) -> None:
        self.exited_with = "UNSET"

    def exit(self, *, exception=None) -> None:
        self.exited_with = exception


class _FakeEvent:
    def __init__(self, text: str = "") -> None:
        self.current_buffer = _FakeBuffer(text)
        self.app = _FakeApp()


def _binding(kb, key_name):
    for b in kb.bindings:
        if [str(k) for k in b.keys] == [key_name]:
            return b
    raise AssertionError(f"no binding for {key_name}")


def test_persistent_ctrl_c_invokes_on_interrupt():
    calls = []
    kb = cli_app._persistent_key_bindings(on_interrupt=lambda: calls.append(1))
    ev = _FakeEvent()
    _binding(kb, "Keys.ControlC").handler(ev)
    assert calls == [1]
    # Ctrl-C must NOT exit the app (it cancels the in-flight turn, keeps the REPL).
    assert ev.app.exited_with == "UNSET"


def test_persistent_ctrl_c_no_handler_is_safe():
    kb = cli_app._persistent_key_bindings(on_interrupt=None)
    ev = _FakeEvent()
    _binding(kb, "Keys.ControlC").handler(ev)  # must not raise
    assert ev.app.exited_with == "UNSET"


def test_persistent_ctrl_d_exits_on_empty_buffer():
    kb = cli_app._persistent_key_bindings()
    ev = _FakeEvent(text="")
    _binding(kb, "Keys.ControlD").handler(ev)
    assert ev.app.exited_with is EOFError


def test_persistent_ctrl_d_ignored_with_text():
    kb = cli_app._persistent_key_bindings()
    ev = _FakeEvent(text="not empty")
    _binding(kb, "Keys.ControlD").handler(ev)
    assert ev.app.exited_with == "UNSET"


class _FakeRenderer:
    def __init__(self) -> None:
        self.cleared = 0

    def clear(self) -> None:
        self.cleared += 1


def test_persistent_ctrl_l_forces_full_repaint():
    kb = cli_app._persistent_key_bindings(on_interrupt=None)
    ev = _FakeEvent()
    ev.app.renderer = _FakeRenderer()
    _binding(kb, "Keys.ControlL").handler(ev)
    assert ev.app.renderer.cleared == 1
    # Ctrl-L must not exit the app or touch the buffer.
    assert ev.app.exited_with == "UNSET"


# ---------------------------------------------------------------------------
# Gate: persistent input is ON by default (the pinned-bottom UX); opt-OUT only
# ---------------------------------------------------------------------------


def test_persistent_input_enabled_by_default(monkeypatch):
    monkeypatch.delenv("POLYROB_PERSISTENT_INPUT", raising=False)
    assert cli_app.persistent_input_enabled() is True


def test_persistent_input_opt_out(monkeypatch):
    for val in ("0", "false", "off", "no"):
        monkeypatch.setenv("POLYROB_PERSISTENT_INPUT", val)
        assert cli_app.persistent_input_enabled() is False, val


def test_persistent_input_explicit_on(monkeypatch):
    monkeypatch.setenv("POLYROB_PERSISTENT_INPUT", "1")
    assert cli_app.persistent_input_enabled() is True


# ---------------------------------------------------------------------------
# End-to-end: drive the real run_async event loop headlessly (pipe + Vt100).
# Verifies the bordered box renders, input submits, and Ctrl-D exits cleanly —
# far stronger than constructing the app.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_async_renders_box_submits_and_exits():
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output.vt100 import Vt100_Output

    state = SessionState()
    state.model = "z-ai/glm-5.2"
    state.provider = "openrouter"
    state.status = "running"
    submitted: list[str] = []

    with create_pipe_input() as pipe:
        sink = io.StringIO()
        out = Vt100_Output(sink, lambda: Size(rows=24, columns=90))
        app, _buf = cli_app.build_app(
            state,
            on_submit=lambda t: submitted.append(t),
            output=out,
            input=pipe,
        )

        async def driver() -> None:
            await asyncio.sleep(0.1)
            pipe.send_text("hello there")   # type into the box
            await asyncio.sleep(0.1)
            pipe.send_text("\r")            # Enter → submit + clear
            await asyncio.sleep(0.1)
            pipe.send_text("\x04")          # Ctrl-D on empty buffer → exit

        task = asyncio.create_task(driver())
        try:
            await asyncio.wait_for(app.run_async(), timeout=5)
        except (EOFError, asyncio.TimeoutError):
            pass
        await task

        assert submitted == ["hello there"]
        clean = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", sink.getvalue())
        # Bug B — thin growing input: a separator rule + caret + hint render, and
        # there is NO 4-sided box (no vertical border glyph).
        assert "─" in clean   # the separator rule above the input
        assert "❯" in clean   # the input caret
        assert "send" in clean  # the keybinding hint
        assert "│" not in clean  # the heavy Frame box is gone


@pytest.mark.asyncio
async def test_input_region_is_compact_not_stretched():
    """Bug B residue: the input must NOT be greedy.

    Given a tall terminal, the bottom region (separator · caret · status · hint)
    must occupy only a handful of rows — the input window must not absorb the free
    vertical space and shove the status/hint to the far bottom behind a big gap.
    Before ``dont_extend_height=True`` the empty input absorbed up to its max
    (content reached row 12); now it stays pinned to content.
    """
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition

    with create_pipe_input() as pipe:
        state = SessionState()
        state.model = "glm"
        state.status = "ready"
        app, _buf = cli_app.build_app(
            state, on_submit=lambda t: None, output=DummyOutput(), input=pipe
        )
        app.loop = asyncio.get_running_loop()
        with set_app(app):
            screen = Screen()
            app.layout.container.write_to_screen(
                screen,
                MouseHandlers(),
                WritePosition(xpos=0, ypos=0, width=90, height=40),
                "",
                False,
                None,
            )
            rows = [
                r
                for r, line in screen.data_buffer.items()
                if any(cell.char.strip() for cell in line.values())
            ]
            assert rows, "the bottom region rendered nothing"
            # 7 = spacer + separator + input + status + autonomy + hint rows
            assert max(rows) <= 7, f"input stretched — content reached row {max(rows)}"
