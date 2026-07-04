"""Arrow-key + fuzzy model selector (Claude-Code style). No numbers.

Why this exists
---------------
The old picker (:mod:`cli.ui.pick`) printed a numbered menu with ``click.echo``
and read the answer with ``input()``. Inside the persistent REPL the whole
process runs under ``patch_stdout(raw=True)`` (``cli/commands/chat.py``), so
``sys.stdout`` is a prompt_toolkit ``StdoutProxy`` that *queues* writes for a
flush thread which schedules the real terminal write as a callback on the event
loop. The picker ran through ``run_in_terminal(..., in_executor=False)`` — i.e.
synchronously on the event-loop thread — and then blocked on ``input()``. With
the loop blocked, the queued ``click.echo`` menu lines could never flush, while
the ``input()`` prompt reached the terminal directly via readline. Net effect:
the prompt showed with **no menu above it** (the reported bug), and the blocking
read froze the REPL until the user typed.

This module replaces that with a proper interactive selector built on the
prompt_toolkit primitives the REPL already uses — arrow keys / Ctrl-N/P to move,
type-to-filter (fuzzy subsequence), Enter to select, Esc to cancel, and a
``＋ custom`` row for an arbitrary ``provider/model``. Nothing is printed through
``StdoutProxy``: the list is a real widget the running Application draws, so it
can't be swallowed.

Two entry points share one core (:class:`PickerModel` + :func:`render_lines`):

* :func:`attach_to_app` — embeds the picker in the persistent REPL
  ``Application`` as a conditional list above the input box, using the app's own
  input buffer as the search field. Resolved via an ``asyncio.Future`` so it runs
  entirely on the event loop and never blocks stdin.
* :func:`run_standalone` — a throwaway ``Application`` for
  ``polyrob model set-default`` (no args) and the legacy (non-persistent) REPL.

Both return ``(provider, model)`` or ``None`` and preserve the old contract:
TTY-safe (a non-TTY caller never prompts — it gets the resolved default), the
``★`` default preselect, capability/pricing hints, and the custom escape hatch.
"""
from __future__ import annotations

import sys
from typing import Callable, List, Optional, Tuple

from modules.llm.available_models import ModelChoice, available_models, steer_notes

#: Sentinel row that always trails the filtered list: pick it to enter a custom
#: ``provider/model`` string (parsed from the current search text).
CUSTOM = "__custom__"

#: Max model rows shown at once; the view scrolls around the cursor past this.
MAX_VISIBLE = 12


# ---------------------------------------------------------------------------
# Pure core — filtering, cursor, selection (no prompt_toolkit; unit-testable)
# ---------------------------------------------------------------------------


def _fuzzy_match(query: str, choice: ModelChoice) -> bool:
    """Subsequence fuzzy match over ``provider display_name model``.

    Empty query matches everything. Case-insensitive; spaces in the query are
    ignored so ``gpt5`` matches ``GPT 5``.
    """
    q = query.strip().lower().replace(" ", "")
    if not q:
        return True
    hay = f"{choice.provider} {choice.display_name} {choice.model}".lower()
    i = 0
    for ch in hay:
        if ch == q[i]:
            i += 1
            if i == len(q):
                return True
    return False


def parse_custom(text: str) -> Optional[Tuple[str, str]]:
    """Parse ``provider/model`` (or ``provider model``) → ``(provider, model)``.

    Returns ``None`` when either half is missing so a stray Enter on the custom
    row with no text is a no-op rather than a bogus selection.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    sep = "/" if "/" in raw else None
    if sep:
        prov, _, mod = raw.partition("/")
    else:
        parts = raw.split(None, 1)
        if len(parts) != 2:
            return None
        prov, mod = parts
    prov, mod = prov.strip(), mod.strip()
    return (prov, mod) if prov and mod else None


class PickerModel:
    """Selection state: the choice list, the live query, and the cursor.

    The cursor indexes into :meth:`selectable` — the filtered choices plus the
    trailing :data:`CUSTOM` sentinel — so navigation and selection stay in sync
    with whatever the current query filters to.
    """

    def __init__(self, choices: List[ModelChoice], default_idx: int = 0) -> None:
        self.choices = list(choices)
        self.default_idx = default_idx if 0 <= default_idx < len(choices) else 0
        self.query = ""
        self.cursor = 0
        # Start the cursor on the flagged default within the (unfiltered) list.
        default_choice = self.choices[self.default_idx] if self.choices else None
        if default_choice is not None:
            sel = self.selectable()
            for i, item in enumerate(sel):
                if item is default_choice:
                    self.cursor = i
                    break

    # -- query/cursor -------------------------------------------------------

    def set_query(self, text: str) -> None:
        """Update the filter text and clamp the cursor back into range."""
        self.query = text or ""
        self.cursor = 0

    def selectable(self) -> List[object]:
        """Filtered ``ModelChoice`` rows (registration order) + ``CUSTOM``."""
        rows: List[object] = [c for c in self.choices if _fuzzy_match(self.query, c)]
        rows.append(CUSTOM)
        return rows

    def move(self, delta: int) -> None:
        n = len(self.selectable())
        if n:
            self.cursor = max(0, min(n - 1, self.cursor + delta))

    def current(self) -> object:
        sel = self.selectable()
        if not sel:
            return CUSTOM
        idx = max(0, min(len(sel) - 1, self.cursor))
        return sel[idx]

    def default_choice(self) -> Optional[ModelChoice]:
        return self.choices[self.default_idx] if self.choices else None

    def selection(self) -> Optional[Tuple[str, str]]:
        """Resolve the current row to ``(provider, model)`` or ``None``.

        ``None`` means "don't resolve yet" — an empty custom row. A real model
        row always resolves.
        """
        item = self.current()
        if item is CUSTOM:
            return parse_custom(self.query)
        return (item.provider, item.model)  # type: ignore[union-attr]


def _caps(choice: ModelChoice) -> str:
    return ",".join(
        t for t, on in (("tools", choice.supports_tools), ("vision", choice.supports_vision)) if on
    )


def _window(items: List[object], cursor: int, size: int) -> Tuple[int, int]:
    """Return the ``[start, end)`` slice of *items* to render around *cursor*."""
    n = len(items)
    if n <= size:
        return 0, n
    start = max(0, min(cursor - size // 2, n - size))
    return start, start + size


def render_lines(model: PickerModel, notes: List[str]) -> List[Tuple[str, str]]:
    """Build the picker body as a list of ``(style, text)`` lines (no newlines).

    Pure over *model*/*notes*; :func:`_to_formatted_text` joins it and
    :func:`line_count` measures it for the window height.
    """
    lines: List[Tuple[str, str]] = [
        ("class:picker.hint", "  type to filter · ↑↓ move · ⏎ select · esc cancel"),
    ]
    items = model.selectable()
    default = model.default_choice()
    start, end = _window(items, model.cursor, MAX_VISIBLE)
    last_provider: Optional[str] = None
    real_rows = 0
    for i in range(start, end):
        item = items[i]
        selected = i == model.cursor
        pointer = "▸" if selected else " "
        style = "class:picker.sel" if selected else "class:picker.row"
        if item is CUSTOM:
            q = model.query.strip()
            tail = q if q else "type provider/model then Enter"
            lines.append((style, f"  {pointer} ＋ custom: {tail}"))
            continue
        real_rows += 1
        choice: ModelChoice = item  # type: ignore[assignment]
        if choice.provider != last_provider:
            lines.append(("class:picker.group", f"  {choice.provider}"))
            last_provider = choice.provider
        star = "★" if choice is default else " "
        caps = _caps(choice)
        cap_str = f"  [{caps}]" if caps else ""
        lines.append(
            (style, f"    {pointer} {star} {choice.display_name}   {choice.pricing_hint}{cap_str}")
        )
    if real_rows == 0:
        lines.append(("class:picker.hint", "  (no match — Enter on ＋custom to type one)"))
    for note in notes:
        lines.append(("class:picker.note", "  " + note))
    return lines


def line_count(model: PickerModel, notes: List[str]) -> int:
    return len(render_lines(model, notes))


def _to_formatted_text(lines: List[Tuple[str, str]]):
    from prompt_toolkit.formatted_text import FormattedText

    ft: List[Tuple[str, str]] = []
    for i, (style, text) in enumerate(lines):
        if i:
            ft.append(("", "\n"))
        ft.append((style, text))
    return FormattedText(ft)


def picker_style_dict() -> dict:
    """Style classes for the selector (merged into the app style)."""
    return {
        "picker.hint": "ansibrightblack",
        "picker.note": "ansiyellow",
        "picker.group": "ansicyan bold",
        "picker.row": "",
        "picker.sel": "reverse bold",
    }


# ---------------------------------------------------------------------------
# Shared key bindings
# ---------------------------------------------------------------------------


def bind_navigation(kb, get_model: Callable[[], Optional[PickerModel]],
                    resolve: Callable[[Optional[Tuple[str, str]]], None],
                    *, active_filter, enter_custom: Optional[Callable[[], None]] = None) -> None:
    """Add move/select/cancel bindings to *kb*, gated on *active_filter*.

    *resolve* is called with the chosen ``(provider, model)`` or ``None`` (cancel).
    A real-model Enter always resolves; an empty custom-row Enter is a no-op.
    """

    @kb.add("up", filter=active_filter)
    @kb.add("c-p", filter=active_filter)
    def _up(event) -> None:
        m = get_model()
        if m is not None:
            m.move(-1)

    @kb.add("down", filter=active_filter)
    @kb.add("c-n", filter=active_filter)
    def _down(event) -> None:
        m = get_model()
        if m is not None:
            m.move(1)

    @kb.add("enter", filter=active_filter, eager=True)
    def _enter(event) -> None:
        m = get_model()
        if m is None:
            resolve(None)
            return
        if m.current() is CUSTOM:
            parsed = parse_custom(m.query)
            if parsed is not None:
                resolve(parsed)
            # empty custom row → ignore Enter (stay open)
            return
        resolve(m.selection())

    @kb.add("escape", filter=active_filter)
    @kb.add("c-c", filter=active_filter)
    @kb.add("c-g", filter=active_filter)
    def _cancel(event) -> None:
        resolve(None)

    if enter_custom is not None:
        @kb.add("c-o", filter=active_filter)
        def _custom(event) -> None:
            enter_custom()


# ---------------------------------------------------------------------------
# Entry point 1 — embed in the persistent REPL Application
# ---------------------------------------------------------------------------


class ReplPicker:
    """Picker embedded in the persistent bottom-anchored REPL Application.

    Renders as a conditional list ABOVE the input box; the app's own input
    buffer doubles as the search field (typing filters live). Resolution is via
    an ``asyncio.Future`` so ``/model`` awaits the choice without blocking the
    event loop or touching ``StdoutProxy``.
    """

    def __init__(self, input_buffer) -> None:
        self.input_buffer = input_buffer
        self.model: Optional[PickerModel] = None
        self.notes: List[str] = []
        self.active = False
        self.app = None
        self._future = None
        self._saved_text = ""

    # -- layout / bindings (built once in build_app) ------------------------

    def container(self):
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.layout.containers import ConditionalContainer, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension

        def _text():
            if self.model is None:
                return _to_formatted_text([])
            return _to_formatted_text(render_lines(self.model, self.notes))

        def _height():
            if not self.active or self.model is None:
                return Dimension(min=0, max=0, preferred=0)
            return Dimension.exact(line_count(self.model, self.notes))

        win = Window(FormattedTextControl(_text), height=_height,
                     wrap_lines=False, dont_extend_height=True)
        return ConditionalContainer(win, filter=Condition(lambda: self.active))

    def key_bindings(self):
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings

        kb = KeyBindings()
        active = Condition(lambda: self.active)
        bind_navigation(kb, lambda: self.model, self._resolve, active_filter=active)
        return kb

    def is_active(self) -> bool:
        return self.active

    def on_search_changed(self) -> None:
        """Wire to ``input_buffer.on_text_changed`` — refilter while active."""
        if self.active and self.model is not None:
            self.model.set_query(self.input_buffer.text)
            if self.app is not None:
                self.app.invalidate()

    # -- open/close ---------------------------------------------------------

    def _resolve(self, value: Optional[Tuple[str, str]]) -> None:
        if self._future is not None and not self._future.done():
            self._future.set_result(value)

    async def open(self, choices: List[ModelChoice], default_idx: int,
                   notes: List[str]) -> Optional[Tuple[str, str]]:
        import asyncio

        # Reentrancy guard: a second open() would overwrite self._future and leave
        # the first await hanging forever. Not reachable via normal UX (Enter is
        # captured by the open picker), but cheap defense-in-depth.
        if self.active:
            return None

        self.model = PickerModel(choices, default_idx)
        self.notes = list(notes)
        self.active = True
        self._saved_text = self.input_buffer.text
        # Clearing the buffer both empties the search and fires on_search_changed.
        self.input_buffer.text = ""
        self.model.set_query("")
        # open() is only ever awaited, so there is always a running loop.
        self._future = asyncio.get_running_loop().create_future()
        if self.app is not None:
            self.app.invalidate()
        try:
            return await self._future
        finally:
            self.active = False
            self.model = None
            # Restore whatever the user was typing before /model.
            self.input_buffer.text = self._saved_text
            if self.app is not None:
                self.app.invalidate()


# ---------------------------------------------------------------------------
# Entry point 2 — standalone throwaway Application
# ---------------------------------------------------------------------------


def _resolved_default(env, choices: List[ModelChoice]) -> Optional[Tuple[str, str]]:
    """Best-effort ``(provider, model)`` preselect from the shared resolver.

    Reused contract from the old picker: resolve the runtime provider, then map a
    provider-only result to that provider's flagged default (not registration
    order). Fail-open to ``None`` (no preselect) on any resolver drift.
    """
    try:
        from core.runtime_config import resolve_runtime_config

        p, m = resolve_runtime_config(None, None, env=env)
    except Exception:
        return None
    if not p:
        return None
    if m:
        return (p, m)
    dc = next((c for c in choices if c.provider == p and c.is_default), None)
    return (dc.provider, dc.model) if dc else None


def _default_idx(choices: List[ModelChoice], default: Optional[Tuple[str, str]]) -> int:
    return next(
        (i for i, c in enumerate(choices) if default and (c.provider, c.model) == tuple(default)),
        0,
    )


def _build_standalone(env, preselect, non_tty_default, isatty_fn, input, output):
    """Shared setup for the standalone picker.

    Returns ``("resolved", value)`` for a non-interactive short-circuit (no
    models, or a non-TTY caller), or ``("app", app)`` — a throwaway Application to
    run (sync via ``.run()`` or async via ``.run_async()``). Factoring this out is
    what lets the sync (``polyrob model set-default``, no running loop) and async
    (in-REPL fallback, loop already running) entry points share one implementation
    without either calling ``asyncio.run()`` from inside a live loop.
    """
    import click

    isatty = isatty_fn if isatty_fn is not None else sys.stdin.isatty
    choices = available_models(env)
    if not choices:
        for n in steer_notes(env):
            click.echo(click.style(n, fg="yellow"), err=True)
        return ("resolved", None)

    default = preselect or _resolved_default(env, choices)
    d_idx = _default_idx(choices, default)

    # Only the injected-input (test) path may bypass the TTY gate.
    if input is None and not isatty():
        return ("resolved", non_tty_default or (choices[d_idx].provider, choices[d_idx].model))

    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.styles import Style

    model = PickerModel(choices, d_idx)
    notes = steer_notes(env)
    search = Buffer(multiline=False)
    search.on_text_changed += lambda _: model.set_query(search.text)

    def _resolve(value):
        app.exit(result=value)

    list_win = Window(
        FormattedTextControl(lambda: _to_formatted_text(render_lines(model, notes))),
        wrap_lines=False, dont_extend_height=True,
    )
    prompt_win = Window(
        BufferControl(buffer=search, input_processors=[]),
        get_line_prefix=lambda *a, **k: FormattedText([("class:picker.group", "  search ❯ ")]),
        height=1, dont_extend_height=True,
    )

    kb = KeyBindings()
    always = Condition(lambda: True)
    bind_navigation(kb, lambda: model, _resolve, active_filter=always)

    app = Application(
        layout=Layout(HSplit([list_win, prompt_win]), focused_element=prompt_win),
        key_bindings=kb,
        style=Style.from_dict(picker_style_dict()),
        full_screen=False,
        erase_when_done=True,
        input=input,
        output=output,
    )
    return ("app", app)


def run_standalone(env=None, *, preselect: Optional[Tuple[str, str]] = None,
                   non_tty_default: Optional[Tuple[str, str]] = None,
                   isatty_fn: Optional[Callable[[], bool]] = None,
                   input=None, output=None) -> Optional[Tuple[str, str]]:
    """Pick ``(provider, model)`` via a throwaway Application — SYNC.

    For callers with NO running event loop (``polyrob model set-default``, tests).
    NEVER call this from inside a running asyncio loop — ``Application.run()`` uses
    ``asyncio.run()`` and would raise; use :func:`run_standalone_async` there.
    TTY-safe: a non-TTY caller returns the resolved default without prompting.
    """
    kind, payload = _build_standalone(env, preselect, non_tty_default, isatty_fn, input, output)
    if kind == "resolved":
        return payload
    return payload.run()


async def run_standalone_async(env=None, *, preselect: Optional[Tuple[str, str]] = None,
                               non_tty_default: Optional[Tuple[str, str]] = None,
                               isatty_fn: Optional[Callable[[], bool]] = None,
                               input=None, output=None) -> Optional[Tuple[str, str]]:
    """Pick ``(provider, model)`` via a throwaway Application — ASYNC.

    For callers ALREADY inside a running event loop (the in-REPL ``/model``
    fallback when no persistent app / picker is available — legacy ``prompt_async``
    and ``--plain`` modes). Uses ``await app.run_async()`` so it never triggers the
    ``asyncio.run() cannot be called from a running event loop`` crash the sync
    variant would.
    """
    kind, payload = _build_standalone(env, preselect, non_tty_default, isatty_fn, input, output)
    if kind == "resolved":
        return payload
    return await payload.run_async()
