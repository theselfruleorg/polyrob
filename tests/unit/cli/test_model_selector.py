"""Tests for the arrow-key/fuzzy model selector (cli/ui/model_selector.py).

Covers the pure core (filter/cursor/selection/render) and the two live paths
driven HEADLESSLY through a prompt_toolkit PipeInput + DummyOutput — no TTY, so
these run in CI. The regression these lock in: the selector must render its list
and resolve a (provider, model) WITHOUT click.echo/input()/StdoutProxy (the old
picker showed no menu inside the persistent REPL).
"""
from __future__ import annotations

import asyncio

import pytest

from cli.ui import model_selector as ms
from cli.ui.model_selector import CUSTOM, PickerModel, parse_custom, render_lines
from modules.llm.available_models import ModelChoice


def _run(coro):
    """Run *coro* in a dedicated loop, then restore a fresh global loop.

    Bare ``asyncio.run()`` sets the current event loop to ``None`` on exit, which
    breaks later ``get_event_loop()``-based tests sharing this process (pytest-asyncio
    strict mode). Restoring a usable loop keeps the rest of the CLI suite green.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _mc(provider, model, name, is_default=False):
    return ModelChoice(
        provider=provider, model=model, display_name=name, is_default=is_default,
        context_window=128000, pricing_hint="$1 / $2 per 1M (indicative)",
        supports_vision=False, supports_tools=True,
    )


CHOICES = [
    _mc("openai", "gpt-5", "GPT-5", is_default=True),
    _mc("openai", "gpt-5.1", "GPT-5.1"),
    _mc("anthropic", "claude-opus-4-8", "Claude Opus 4.8"),
    _mc("openrouter", "z-ai/glm-5.2", "GLM 5.2"),
]


# ---- pure core ------------------------------------------------------------


def test_default_preselect_cursor_lands_on_flagged_default():
    m = PickerModel(CHOICES, default_idx=0)
    assert m.current() is CHOICES[0]
    assert m.selection() == ("openai", "gpt-5")


def test_empty_query_lists_all_plus_custom_row():
    m = PickerModel(CHOICES)
    sel = m.selectable()
    assert sel[-1] is CUSTOM
    assert [c for c in sel if c is not CUSTOM] == CHOICES


def test_fuzzy_filter_narrows_and_is_subsequence():
    m = PickerModel(CHOICES)
    m.set_query("glm")
    rows = [c for c in m.selectable() if c is not CUSTOM]
    assert rows == [CHOICES[3]]
    m.set_query("opus")
    rows = [c for c in m.selectable() if c is not CUSTOM]
    assert rows == [CHOICES[2]]
    # subsequence: 'g5' matches GPT-5 and GLM 5.2 (g..5)
    m.set_query("gpt5")
    rows = [c for c in m.selectable() if c is not CUSTOM]
    assert CHOICES[0] in rows


def test_arrow_move_clamps_within_bounds():
    m = PickerModel(CHOICES, default_idx=0)
    m.cursor = 0
    m.move(-1)
    assert m.cursor == 0  # clamped at top
    for _ in range(100):
        m.move(1)
    assert m.cursor == len(m.selectable()) - 1  # clamped at custom row


def test_custom_row_selection_parses_query():
    m = PickerModel(CHOICES)
    m.set_query("myprov/my-model")
    # move cursor to the custom (last) row
    m.cursor = len(m.selectable()) - 1
    assert m.current() is CUSTOM
    assert m.selection() == ("myprov", "my-model")


def test_parse_custom_accepts_slash_and_space_rejects_partial():
    assert parse_custom("openai/gpt-5") == ("openai", "gpt-5")
    assert parse_custom("openai gpt-5") == ("openai", "gpt-5")
    assert parse_custom("openai") is None
    assert parse_custom("") is None
    assert parse_custom("   ") is None


def test_render_shows_star_pointer_and_no_numbers():
    m = PickerModel(CHOICES, default_idx=0)
    text = "\n".join(t for _, t in render_lines(m, notes=[]))
    assert "★" in text            # default marked
    assert "▸" in text            # cursor pointer
    assert "GPT-5" in text and "GLM 5.2" in text
    assert "＋ custom" in text
    # No "1)" "2)" numbered-menu artifacts
    assert "1)" not in text and "2)" not in text


def test_render_no_match_hint_when_filter_excludes_all():
    m = PickerModel(CHOICES)
    m.set_query("zzzzzznope")
    text = "\n".join(t for _, t in render_lines(m, notes=[]))
    assert "no match" in text


# ---- live standalone path (headless PipeInput) ----------------------------


def _run_standalone_with_keys(monkeypatch, keys: str):
    """Drive run_standalone through a pipe input, returning its (provider,model)|None."""
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    monkeypatch.setattr(ms, "available_models", lambda env=None: CHOICES)
    monkeypatch.setattr(ms, "steer_notes", lambda env=None: [])
    monkeypatch.setattr(ms, "_resolved_default", lambda env, choices: None)

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return ms.run_standalone(isatty_fn=lambda: True, input=pipe, output=DummyOutput())


def test_standalone_enter_selects_default(monkeypatch):
    # Cursor starts on the flagged default (gpt-5); bare Enter selects it.
    result = _run_standalone_with_keys(monkeypatch, "\r")
    assert result == ("openai", "gpt-5")


def test_standalone_arrow_down_then_enter(monkeypatch):
    # Down once from gpt-5 → gpt-5.1.
    result = _run_standalone_with_keys(monkeypatch, "\x1b[B\r")  # ESC [ B = Down
    assert result == ("openai", "gpt-5.1")


def test_standalone_type_to_filter_then_enter(monkeypatch):
    # Typing 'glm' filters to GLM 5.2 (cursor resets to row 0), Enter selects it.
    result = _run_standalone_with_keys(monkeypatch, "glm\r")
    assert result == ("openrouter", "z-ai/glm-5.2")


def test_standalone_ctrl_c_cancels(monkeypatch):
    result = _run_standalone_with_keys(monkeypatch, "\x03")  # Ctrl-C
    assert result is None


def test_standalone_non_tty_returns_default_without_prompting(monkeypatch):
    monkeypatch.setattr(ms, "available_models", lambda env=None: CHOICES)
    monkeypatch.setattr(ms, "steer_notes", lambda env=None: [])
    monkeypatch.setattr(ms, "_resolved_default", lambda env, choices: None)
    # No input pipe + isatty False → returns the default index (gpt-5), no app.
    result = ms.run_standalone(isatty_fn=lambda: False)
    assert result == ("openai", "gpt-5")


def test_standalone_no_models_returns_none(monkeypatch):
    monkeypatch.setattr(ms, "available_models", lambda env=None: [])
    monkeypatch.setattr(ms, "steer_notes", lambda env=None: [])
    result = ms.run_standalone(isatty_fn=lambda: True)
    assert result is None


# ---- embedded REPL path (ReplPicker driven on a real event loop) ----------


def test_repl_picker_open_resolves_on_enter():
    """ReplPicker.open awaits a Future; simulate the app's key-binding resolving it."""
    from prompt_toolkit.buffer import Buffer

    buf = Buffer(multiline=False)
    picker = ms.ReplPicker(buf)
    buf.on_text_changed += lambda _: picker.on_search_changed()

    async def scenario():
        task = asyncio.ensure_future(picker.open(CHOICES, default_idx=0, notes=[]))
        await asyncio.sleep(0)  # let open() install the future + activate
        assert picker.active is True
        # Simulate a down-arrow then Enter via the model + resolve seam.
        picker.model.move(1)  # → gpt-5.1
        picker._resolve(picker.model.selection())
        return await task

    result = _run(scenario())
    assert result == ("openai", "gpt-5.1")
    assert picker.active is False  # closed + state cleared


def test_run_standalone_async_works_inside_running_loop(monkeypatch):
    """Regression: the in-REPL /model fallback runs inside the REPL's event loop.
    The sync run_standalone (asyncio.run) crashes there; run_standalone_async must
    resolve via app.run_async() with no 'loop already running' error."""
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    monkeypatch.setattr(ms, "available_models", lambda env=None: CHOICES)
    monkeypatch.setattr(ms, "steer_notes", lambda env=None: [])
    monkeypatch.setattr(ms, "_resolved_default", lambda env, choices: None)

    async def scenario():
        with create_pipe_input() as pipe:
            pipe.send_text("\r")  # Enter selects the default (gpt-5)
            return await ms.run_standalone_async(
                isatty_fn=lambda: True, input=pipe, output=DummyOutput())

    result = _run(scenario())  # a running loop inside
    assert result == ("openai", "gpt-5")


def test_run_standalone_sync_raises_inside_running_loop(monkeypatch):
    """The sync variant must NOT be used inside a loop — documents why the async
    variant exists (calling it from a running loop is the bug we fixed)."""
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    monkeypatch.setattr(ms, "available_models", lambda env=None: CHOICES)
    monkeypatch.setattr(ms, "steer_notes", lambda env=None: [])
    monkeypatch.setattr(ms, "_resolved_default", lambda env, choices: None)

    async def scenario():
        with create_pipe_input() as pipe:
            pipe.send_text("\r")
            return ms.run_standalone(isatty_fn=lambda: True, input=pipe, output=DummyOutput())

    with pytest.raises(RuntimeError):
        _run(scenario())


def test_repl_picker_open_reentrancy_guard():
    """A second open() while already active returns None, not a hung future."""
    from prompt_toolkit.buffer import Buffer

    buf = Buffer(multiline=False)
    picker = ms.ReplPicker(buf)

    async def scenario():
        task = asyncio.ensure_future(picker.open(CHOICES, default_idx=0, notes=[]))
        await asyncio.sleep(0)
        assert picker.active is True
        second = await picker.open(CHOICES, default_idx=0, notes=[])  # reentrant
        assert second is None  # guarded, did not clobber the first future
        picker._resolve(("openai", "gpt-5"))
        return await task

    assert _run(scenario()) == ("openai", "gpt-5")


def test_repl_picker_restores_buffer_text_on_close():
    from prompt_toolkit.buffer import Buffer

    buf = Buffer(multiline=False)
    buf.text = "half-typed question"
    picker = ms.ReplPicker(buf)
    buf.on_text_changed += lambda _: picker.on_search_changed()

    async def scenario():
        task = asyncio.ensure_future(picker.open(CHOICES, default_idx=0, notes=[]))
        await asyncio.sleep(0)
        assert buf.text == ""  # cleared for the search field
        picker._resolve(None)  # cancel
        await task

    _run(scenario())
    assert buf.text == "half-typed question"  # restored
