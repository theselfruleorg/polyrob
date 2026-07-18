"""app.py — prompt_toolkit PromptSession setup for the POLYROB CLI (Phase 2).

Builds the interactive input surface:
- ``PromptSession`` with ``FileHistory(~/.polyrob/history)``.
- A ``bottom_toolbar`` callable that pulls live metrics from ``SessionState``
  (via ``statusbar.status_formatted``) and ticks a spinner.
- Key bindings:
    * ``Enter``      → submit
    * ``Meta+Enter`` (Alt/Esc-Enter) → insert newline (multi-line input)
    * ``Ctrl-C``     → interrupt the current turn (keeps the loop)
    * ``Ctrl-D``     → exit
- ``prompt_async()`` for the input read (the REPL already runs under asyncio —
  we do NOT start a second loop; proposal §14).

Slash autocomplete is Phase 4 — a ``completer`` seam is exposed (param) but no
registry is built here.

Everything is factored so the prompt_toolkit objects can be constructed
headlessly in tests (no TTY needed); only ``prompt_async`` requires a terminal.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from cli.ui import statusbar
from cli.ui.state import SessionState
from cli.ui.theme import ICONS, SPINNER_FRAMES

if TYPE_CHECKING:  # pragma: no cover - typing only
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer
    from prompt_toolkit.key_binding import KeyBindings


from core.paths import polyrob_home
DEFAULT_HISTORY_PATH = polyrob_home() / "history"


def default_history_path() -> Path:
    """Return (and ensure the parent of) the default history file path."""
    DEFAULT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DEFAULT_HISTORY_PATH


# ---------------------------------------------------------------------------
# Key bindings
# ---------------------------------------------------------------------------


def build_key_bindings() -> "KeyBindings":
    """Construct the REPL key bindings.

    - ``Enter`` submits the buffer.
    - ``Meta+Enter`` (``escape, enter``) inserts a newline (multi-line input).
    - ``Ctrl-C`` raises ``KeyboardInterrupt`` so the loop can interrupt the
      current turn without exiting.
    - ``Ctrl-D`` on an empty buffer raises ``EOFError`` to exit.
    """
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("enter")
    def _(event: Any) -> None:
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    @kb.add("c-c")
    def _(event: Any) -> None:
        # Surface as KeyboardInterrupt; the REPL catches it and keeps looping.
        event.app.exit(exception=KeyboardInterrupt)

    @kb.add("c-d")
    def _(event: Any) -> None:
        if not event.current_buffer.text:
            event.app.exit(exception=EOFError)

    return kb


# ---------------------------------------------------------------------------
# Distinctive input frame (prompt fragments + rprompt)
# ---------------------------------------------------------------------------

#: The left-edge bar glyph shared by the prompt and the bottom toolbar, so the
#: input region reads as one bordered area (a true 4-sided box isn't possible
#: for an INLINE prompt_toolkit prompt without going full-screen, which would
#: forfeit native scrollback — see the CLI review).
BORDER_BAR = "▌"
PROMPT_CARET = "❯"

#: Max rows the thin input grows to before it stops expanding (a long paste can't
#: swallow the whole terminal; the buffer still scrolls internally past this).
MAX_INPUT_ROWS = 10


def input_height_dimension(line_count: int, max_rows: int = MAX_INPUT_ROWS) -> Any:
    """Height for the thin growing input: 1 row when empty, +1 per content line,
    clamped to ``max_rows`` (bug B).

    The input is NOT a fixed-height box — it reserves exactly one row when empty
    and grows as the user types/pastes newlines, the way Claude Code's prompt
    does. Pure (no TTY) so the growth policy is unit-testable.
    """
    from prompt_toolkit.layout.dimension import Dimension

    preferred = min(max(1, line_count), max_rows)
    return Dimension(min=1, max=max_rows, preferred=preferred)


def separator_label(state: SessionState) -> str:
    """The ``rob · model · provider`` label shown on the thin separator rule.

    Replaces the old 4-sided ``Frame`` title — the agent identity + active model
    sit on the rule above the input instead of a heavy box edge. Pure.
    """
    from cli.ui.identity import agent_display_name
    name = agent_display_name()
    model = (state.model or "").strip()
    provider = (state.provider or "").strip()
    label = name
    if model:
        label = f"{name} {ICONS.bullet} {model}"
        if provider:
            label = f"{label} {ICONS.bullet} {provider}"
    return label


def build_prompt_fragments(state: SessionState, *, prompt_text: Optional[str] = None) -> Any:
    """Build the REPL prompt as themeable prompt_toolkit ``FormattedText``.

    Default: a distinctive left-border bar + caret (``▌ ❯ ``) so the input line
    is visually distinct from the conversation scrollback. ``prompt_text`` forces
    a literal string (kept for back-compat / tests). Pure — snapshot-testable
    without a TTY.
    """
    from prompt_toolkit.formatted_text import FormattedText

    if prompt_text is not None:
        return FormattedText([("", prompt_text)])
    return FormattedText(
        [
            ("class:prompt.border", BORDER_BAR),
            ("class:prompt.caret", f" {PROMPT_CARET} "),
        ]
    )


def build_rprompt(state: SessionState) -> Any:
    """Build the right-aligned ``model · provider`` affordance (Cursor-style).

    Returns empty ``FormattedText`` until a model is known (set after the first
    LLM call), so the prompt isn't cluttered at session start. Pure.
    """
    from prompt_toolkit.formatted_text import FormattedText

    model = (state.model or "").strip()
    provider = (state.provider or "").strip()
    if not model and not provider:
        return FormattedText([])
    label = model or "—"
    if provider:
        label = f"{label} {ICONS.bullet} {provider}"
    return FormattedText([("class:prompt.rprompt", f" {label} ")])


# ---------------------------------------------------------------------------
# Bottom toolbar
# ---------------------------------------------------------------------------


def make_bottom_toolbar(
    state: SessionState,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[], Any]:
    """Return a ``bottom_toolbar`` callable that renders live status.

    The callable is invoked by prompt_toolkit on every repaint; it ticks a
    spinner frame from the monotonic clock and delegates formatting to
    ``statusbar.status_formatted``. A leading border bar makes the toolbar read
    as the bottom edge of the framed input region (matching the prompt's bar).
    """
    from prompt_toolkit.formatted_text import FormattedText

    def _toolbar() -> Any:
        frame_idx = int(clock() * 5) % len(SPINNER_FRAMES)
        spinner = ""
        # Spinner is gated on the lifecycle being active (a turn in flight), NOT on
        # the status string — so it can't animate while idle and survives any
        # status-vocabulary change. Defensive getattr for a lifecycle-less state.
        _lc = getattr(state, "lifecycle", None)
        if _lc is not None and _lc.is_active():
            spinner = SPINNER_FRAMES[frame_idx] + " "
        status = statusbar.status_formatted(state, spinner=spinner)
        return FormattedText(
            [("class:prompt.border", BORDER_BAR)] + list(status)
        )

    return _toolbar


def toolbar_style():
    """Return the prompt_toolkit ``Style`` for the bottom toolbar classes."""
    from prompt_toolkit.styles import Style

    return Style.from_dict(
        {
            "bottom-toolbar": "noreverse",
            "toolbar.model": "bold",
            "toolbar.tokens": "",
            "toolbar.ctx": "",
            "toolbar.ctx.warn": "ansiyellow",
            "toolbar.ctx.high": "ansired bold",
            "toolbar.tool": "ansicyan",
            "toolbar.subagents": "ansimagenta",
            "toolbar.cost": "",
            "toolbar.elapsed": "",
            "toolbar.autonomy": "ansibrightblack",
            "status.ok": "ansigreen",
            "status.running": "ansiyellow",
            "status.error": "ansired",
            # Distinctive input frame.
            "prompt.border": "ansicyan bold",
            "prompt.caret": "ansicyan bold",
            "prompt.rprompt": "ansibrightblack",
            "prompt.hint": "ansibrightblack",
            "prompt.hint.tip": "ansibrightblack italic",
            "prompt.frame.title": "ansicyan bold",
            # Slash-command input highlighting (cli/ui/slash_highlight.py).
            "prompt.slash.valid": "ansicyan bold",
            "prompt.slash.partial": "",
            "prompt.slash.unknown": "ansiyellow",
            "prompt.slash.arg": "ansigreen",
            # The bordered input box (opencode-style) — cyan edge, default body.
            "frame.border": "ansicyan",
            "frame.label": "ansicyan bold",
            # Arrow-key model selector (cli/ui/model_selector.py).
            "picker.hint": "ansibrightblack",
            "picker.note": "ansiyellow",
            "picker.group": "ansicyan bold",
            "picker.row": "",
            "picker.sel": "reverse bold",
            # Completion menu (the slash-command palette).
            "completion-menu": "",
            "completion-menu.completion": "",
            "completion-menu.completion.current": "reverse bold",
            "completion-menu.meta.completion": "ansibrightblack",
            "completion-menu.meta.completion.current": "ansibrightblack reverse",
        }
    )


# ---------------------------------------------------------------------------
# PromptSession
# ---------------------------------------------------------------------------


def build_prompt_session(
    state: SessionState,
    *,
    history_path: Optional[Path] = None,
    completer: "Optional[Completer]" = None,
    prompt_text: Optional[str] = None,
    clock: Callable[[], float] = time.monotonic,
) -> "PromptSession":
    """Construct the REPL ``PromptSession`` (no TTY required to build it).

    Args:
        state:        Shared ``SessionState`` (drives prompt/rprompt/toolbar).
        history_path: FileHistory path (default ``~/.polyrob/history``).
        completer:    Optional prompt_toolkit ``Completer`` (Phase 4 seam).
        prompt_text:  Force a literal prompt prefix; ``None`` (default) uses the
                      distinctive framed ``▌ ❯`` prompt + live ``model · provider``
                      rprompt.
        clock:        Monotonic clock (injectable for tests).
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    hpath = history_path or default_history_path()
    hpath.parent.mkdir(parents=True, exist_ok=True)

    # Callables so the prompt frame + rprompt repaint live (the model/provider
    # become known only after the first LLM call).
    return PromptSession(
        message=lambda: build_prompt_fragments(state, prompt_text=prompt_text),
        rprompt=lambda: build_rprompt(state),
        history=FileHistory(str(hpath)),
        key_bindings=build_key_bindings(),
        bottom_toolbar=make_bottom_toolbar(state, clock=clock),
        completer=completer,
        complete_while_typing=False,
        multiline=False,
        style=toolbar_style(),
        refresh_interval=0.2,  # ~5 Hz toolbar repaint for the spinner/elapsed
    )


def make_prompt_reader(session: "PromptSession") -> Callable[[], Any]:
    """Adapt a ``PromptSession`` into the REPL's ``read_line`` async seam.

    Returns a zero-arg coroutine that reads one line via ``prompt_async`` and
    raises ``EOFError`` on Ctrl-D (so the REPL loop breaks cleanly).
    """

    async def read_line() -> str:
        try:
            return await session.prompt_async()
        except EOFError:
            raise
        # KeyboardInterrupt propagates to the loop, which keeps looping.

    return read_line


# ---------------------------------------------------------------------------
# Persistent bottom-anchored Application (D4/D5 — gated POLYROB_PERSISTENT_INPUT)
# ---------------------------------------------------------------------------


def persistent_input_enabled() -> bool:
    """True when the persistent bottom-anchored input is active (default ON).

    The persistent ``Application`` keeps the input box PINNED at the bottom and
    LIVE during a turn (status/spinner repaint, tool output scrolls above) — the
    Claude-Code / opencode experience. It is the default for an interactive TTY;
    set ``POLYROB_PERSISTENT_INPUT=0`` (or ``off``/``false``/``no``) to force the
    legacy ephemeral ``prompt_async`` path. Non-TTY / ``--plain`` always use the
    legacy path regardless (the caller gates on ``is_tty`` + ``not plain``), and a
    build/start failure fails open to it too.
    """
    import os

    return os.environ.get("POLYROB_PERSISTENT_INPUT", "1").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


def build_app(
    state: SessionState,
    *,
    on_submit: Callable[[str], Any],
    completer: "Optional[Completer]" = None,
    on_interrupt: Optional[Callable[[], None]] = None,
    clock: Callable[[], float] = time.monotonic,
    output: Any = None,
    input: Any = None,
) -> Any:
    """Build the persistent, NON-full-screen bottom-anchored ``Application``.

    The layout reserves only the bottom region — an input window (``▌ ❯``), a live
    status window, and a conditional autonomy window. Content (agent messages,
    summaries) keeps scrolling natively ABOVE via ``run_in_terminal`` (so native
    scrollback is preserved — this is NOT a full-screen transcript app). Because
    the ``Application`` runs continuously, the status repaints LIVE during a turn
    (the toolbar-freeze is gone).

    Args:
        state:        Shared ``SessionState`` (drives the status/autonomy lines).
        on_submit:    ``(text) -> None`` called on Enter for a non-blank line. The
                      caller schedules the turn as a background task.
        completer:    Optional slash completer.
        on_interrupt: Optional ``() -> None`` called on Ctrl-C (cancel the turn).
        clock:        Monotonic clock (spinner tick; injectable for tests).
        output/input: prompt_toolkit Output/Input (DummyOutput/pipe in tests).

    Returns ``(app, input_buffer)``. Buildable headlessly (no TTY); ``app.run_async``
    requires a terminal.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.filters import Condition, has_completions
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import merge_key_bindings
    from prompt_toolkit.layout import Dimension, HSplit, Layout, VSplit, Window
    from prompt_toolkit.layout.containers import (
        ConditionalContainer,
        Float,
        FloatContainer,
    )
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.layout.menus import CompletionsMenu

    from cli.ui.model_selector import ReplPicker
    from cli.ui.slash_highlight import SlashHighlightProcessor

    def _accept(buff: Any) -> bool:
        text = buff.text
        if text.strip():
            on_submit(text)
        buff.reset()  # clear the input after submit
        return False  # don't keep the text in the buffer

    input_buffer = Buffer(
        multiline=False,
        accept_handler=_accept,
        completer=completer,
        # Live palette: the menu opens while typing a slash command ONLY — plain
        # chat typing never triggers completion. `picker` is defined below (after
        # this Buffer) but the lambda late-binds it, and it's only ever evaluated
        # on a keystroke once build_app has finished wiring both — so referencing
        # it here is safe. Gated on `not picker.is_active()` so the completion
        # menu doesn't fight the /model picker while it borrows this buffer.
        complete_while_typing=Condition(
            lambda: input_buffer.text.startswith("/") and not picker.is_active()
        ),
    )

    # The arrow-key model selector (/model) renders as a conditional list ABOVE
    # the input and borrows this buffer as its search field while open. It runs
    # entirely on the event loop (resolved via a Future), so it never blocks the
    # REPL or routes through patch_stdout's StdoutProxy (the old picker's bug).
    picker = ReplPicker(input_buffer)
    input_buffer.on_text_changed += lambda _: picker.on_search_changed()

    def _caret_prefix() -> Any:
        # The ❯ caret prefixes every input row (thin design — no box border).
        return FormattedText([("class:prompt.caret", f"{PROMPT_CARET} ")])

    def _separator_label() -> Any:
        # The agent name + active model sit on the thin rule above the input —
        # the heavy 4-sided Frame is gone (bug B).
        return FormattedText(
            [("class:prompt.frame.title", f"─ {separator_label(state)} ")]
        )

    def _input_height() -> Any:
        # 1 row when empty; grows with typed/pasted newlines; clamped.
        return input_height_dimension(input_buffer.document.line_count)

    def _status() -> Any:
        frame_idx = int(clock() * 5) % len(SPINNER_FRAMES)
        spinner = ""
        # Spinner is gated on the lifecycle being active (a turn in flight), NOT on
        # the status string — so it can't animate while idle and survives any
        # status-vocabulary change. Defensive getattr for a lifecycle-less state.
        _lc = getattr(state, "lifecycle", None)
        if _lc is not None and _lc.is_active():
            spinner = SPINNER_FRAMES[frame_idx] + " "
        # Model lives on the box's top edge (the frame title), so omit it here.
        status = statusbar.status_formatted(state, spinner=spinner, include_model=False)
        # Indent one space so the status bar aligns under the box body.
        return FormattedText([("", " ")] + list(status))

    def _hint() -> Any:
        # Context-aware hint row (cli/ui/hints.py): idle keys + rotating tip,
        # a known /cmd's usage while typing it, ^C stop mid-turn. While the
        # /model picker is active it renders its own hint line (it borrows this
        # buffer), so the normal "⏎ send" hint here would be misleading.
        if picker.is_active():
            return FormattedText([])
        from cli.ui import hints

        return FormattedText(hints.hint_fragments(state, input_buffer.text, clock()))

    input_window = Window(
        BufferControl(
            buffer=input_buffer,
            input_processors=[SlashHighlightProcessor(gate=picker.is_active)],
        ),
        get_line_prefix=lambda *a, **k: _caret_prefix(),
        height=_input_height,
        # Without this the input window is GREEDY: in the bottom region it absorbs
        # all free vertical space up to its max, pushing the status/hint rows to
        # the far bottom with a huge empty gap (the "stretched input" bug). Pin it
        # to its content height so the region stays compact and only grows as the
        # user types/pastes.
        dont_extend_height=True,
        wrap_lines=True,
    )
    # A thin titled rule above the input: ``─ rob · model · provider ───────``.
    separator = VSplit(
        [
            Window(
                FormattedTextControl(_separator_label),
                height=1,
                dont_extend_width=True,
            ),
            Window(char="─", height=1, style="class:prompt.border"),
        ]
    )
    status_window = Window(FormattedTextControl(_status), height=1)
    hint_window = Window(FormattedTextControl(_hint), height=1)
    autonomy_window = ConditionalContainer(
        Window(
            FormattedTextControl(
                lambda: FormattedText(
                    [
                        ("", " "),
                        (
                            "class:prompt.rprompt",
                            # Model/provider already live on the separator rule —
                            # repeat only the autonomy half here (no duplication).
                            statusbar.autonomy_line(state, include_model=False),
                        ),
                    ]
                )
            ),
            height=1,
        ),
        filter=Condition(
            lambda: bool(statusbar.autonomy_line(state, include_model=False))
        ),
    )

    _MENU_ROWS = 10
    # Reserve rows under the input while the menu is open (PromptSession's
    # reserve_space_for_menu pattern) — floats don't add preferred height, so
    # without this the bottom-anchored region has no room for the menu.
    menu_space = ConditionalContainer(
        Window(height=Dimension(min=0, max=_MENU_ROWS, preferred=_MENU_ROWS)),
        filter=has_completions,
    )
    body = HSplit([
        # One empty row above the rule: the region owns its breathing room, so
        # the last scrollback line (user echo, tool line, bubble, summary) is
        # never glued to the input box — transcript blocks emit a blank BEFORE
        # themselves only (see blocks.py rhythm note).
        Window(height=1),
        picker.container(),
        separator,
        input_window,
        menu_space,
        status_window,
        autonomy_window,
        hint_window,
    ])
    layout = Layout(
        FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=_MENU_ROWS, scroll_offset=1),
                )
            ],
        )
    )

    kb = merge_key_bindings([
        _persistent_key_bindings(on_interrupt=on_interrupt, is_picker_active=picker.is_active),
        picker.key_bindings(),
    ])

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=toolbar_style(),
        full_screen=False,
        refresh_interval=0.2,
        erase_when_done=True,
        output=output,
        input=input,
    )
    picker.app = app
    app._picker = picker  # /model reaches the picker via get_app_or_none()._picker
    return app, input_buffer


def _persistent_key_bindings(*, on_interrupt: Optional[Callable[[], None]] = None,
                             is_picker_active: Optional[Callable[[], bool]] = None) -> Any:
    """Key bindings for the persistent Application.

    - ``Enter`` submits (the buffer's accept handler runs ``on_submit``).
    - ``Meta+Enter`` inserts a newline (multi-line input).
    - ``Ctrl-C`` calls ``on_interrupt`` (cancel the in-flight turn) without exiting.
    - ``Ctrl-D`` on an empty buffer exits the app (EOF).
    - ``Ctrl-L`` clears the screen and forces a full repaint (corruption recovery).

    ``is_picker_active`` gates every binding on ``~active`` so that while the
    arrow-key model selector is open, Enter/Ctrl-C/Ctrl-D route to the picker's
    own bindings (submit/cancel select a model, not the turn) rather than here.
    Typed characters still flow into the shared buffer — that IS the picker's
    search field.
    """
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()
    _pa = is_picker_active or (lambda: False)
    not_picking = Condition(lambda: not _pa())

    @kb.add("enter", filter=not_picking)
    def _(event: Any) -> None:
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter", filter=not_picking)
    def _(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    @kb.add("c-c", filter=not_picking)
    def _(event: Any) -> None:
        if on_interrupt is not None:
            on_interrupt()

    @kb.add("c-d", filter=not_picking)
    def _(event: Any) -> None:
        if not event.current_buffer.text:
            event.app.exit(exception=EOFError)

    @kb.add("c-l")
    def _(event: Any) -> None:
        # Full clear + repaint (standard shell affordance). Recovers a corrupted
        # scroll region — e.g. a frame stranded by an uncoordinated raw write —
        # without restarting the REPL. Unfiltered: also safe while the picker is
        # open (the repaint redraws the whole layout, picker included).
        event.app.renderer.clear()

    return kb
