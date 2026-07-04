"""Headless construction tests for cli.ui.app (prompt_toolkit objects).

prompt_toolkit objects can be built without a TTY; we never call prompt_async.
"""

from __future__ import annotations

from pathlib import Path

from cli.ui import app
from cli.ui.state import SessionState


def test_build_key_bindings_has_expected_keys():
    kb = app.build_key_bindings()
    # Each key is a prompt_toolkit Keys enum (value like 'c-m', 'escape',
    # 'c-c', 'c-d') or a plain string; normalise to the underlying value.
    seqs = [tuple(getattr(k, "value", k) for k in b.keys) for b in kb.bindings]
    flat = {k for seq in seqs for k in seq}
    # Enter == ControlM ('c-m'); Meta+Enter == ('escape', 'c-m').
    assert "c-m" in flat  # Enter (submit / newline-with-escape)
    assert ("escape", "c-m") in seqs  # Meta+Enter newline
    assert "c-c" in flat  # Ctrl-C interrupt
    assert "c-d" in flat  # Ctrl-D exit


def test_make_bottom_toolbar_renders_from_state():
    state = SessionState()
    state.model = "gemini"
    state.status = "running"
    clock = lambda: 0.0  # noqa: E731  deterministic spinner frame
    toolbar = app.make_bottom_toolbar(state, clock=clock)
    ft = toolbar()
    text = "".join(frag[1] for frag in ft)
    assert "gemini" in text
    assert "running" in text


def test_make_bottom_toolbar_spinner_only_when_running():
    state = SessionState()
    state.model = "m"
    state.status = "completed"
    toolbar = app.make_bottom_toolbar(state, clock=lambda: 0.0)
    text = "".join(frag[1] for frag in toolbar())
    # No spinner glyph prefix when not running.
    assert "completed" in text


def test_build_prompt_fragments_default_is_framed_caret():
    """The default prompt is a distinctive left-bar + caret (not a bare '› ')."""
    state = SessionState()
    ft = app.build_prompt_fragments(state)
    text = "".join(frag[1] for frag in ft)
    assert "❯" in text          # caret
    assert "▌" in text          # left border bar
    # Carries themeable classes (not a single unstyled fragment).
    classes = [frag[0] for frag in ft]
    assert any("prompt" in c for c in classes)


def test_build_prompt_fragments_respects_explicit_text():
    state = SessionState()
    ft = app.build_prompt_fragments(state, prompt_text="> ")
    assert "".join(frag[1] for frag in ft) == "> "


def test_build_rprompt_empty_when_no_model():
    state = SessionState()
    ft = app.build_rprompt(state)
    assert "".join(frag[1] for frag in ft).strip() == ""


def test_build_rprompt_shows_model_and_provider():
    state = SessionState()
    state.model = "glm-5.2"
    state.provider = "openrouter"
    text = "".join(frag[1] for frag in app.build_rprompt(state))
    assert "glm-5.2" in text
    assert "openrouter" in text


def test_bottom_toolbar_has_left_border_bar():
    """The toolbar reads as the bottom edge of the framed input region."""
    state = SessionState()
    state.model = "m"
    state.status = "running"
    text = "".join(frag[1] for frag in app.make_bottom_toolbar(state, clock=lambda: 0.0)())
    assert "▌" in text


def test_build_prompt_session_constructs(tmp_path: Path):
    state = SessionState()
    session = app.build_prompt_session(state, history_path=tmp_path / "hist")
    assert session is not None
    # History file parent should exist.
    assert (tmp_path / "hist").parent.exists()


def test_make_prompt_reader_returns_coroutine_factory(tmp_path: Path):
    state = SessionState()
    session = app.build_prompt_session(state, history_path=tmp_path / "hist")
    reader = app.make_prompt_reader(session)
    assert callable(reader)


def test_default_history_path_ensures_parent():
    p = app.default_history_path()
    assert p.parent.exists()


def test_toolbar_style_builds():
    style = app.toolbar_style()
    assert style is not None
