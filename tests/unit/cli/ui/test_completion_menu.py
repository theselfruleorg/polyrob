"""Completion menu wiring: FloatContainer + CompletionsMenu + scoped complete_while_typing."""
import pytest

from cli.ui.app import build_app
from cli.ui.state import SessionState


def _build():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        app, buf = build_app(
            SessionState(), on_submit=lambda t: None, input=pipe, output=DummyOutput()
        )
        yield app, buf


@pytest.fixture()
def app_buf():
    yield from _build()


def _walk(container):
    yield container
    for child in getattr(container, "get_children", lambda: [])():
        yield from _walk(child)


def test_layout_has_completions_menu(app_buf):
    from prompt_toolkit.layout.containers import FloatContainer
    from prompt_toolkit.layout.menus import CompletionsMenu

    app, _ = app_buf
    floats = [
        f
        for c in _walk(app.layout.container)
        if isinstance(c, FloatContainer)
        for f in c.floats
    ]
    assert any(
        isinstance(w, CompletionsMenu)
        for f in floats
        for w in _walk(f.content)
    )


def test_complete_while_typing_only_for_slash(app_buf):
    _, buf = app_buf
    buf.text = "/mo"
    assert bool(buf.complete_while_typing()) is True
    buf.text = "hello"
    assert bool(buf.complete_while_typing()) is False
    buf.text = ""
    assert bool(buf.complete_while_typing()) is False


def test_complete_while_typing_gated_by_picker_active(app_buf):
    # While the /model picker borrows the buffer, the "/" completion menu must
    # not also fire against the picker's own search text.
    app, buf = app_buf
    buf.text = "/mo"
    assert bool(buf.complete_while_typing()) is True

    app._picker.active = True
    assert bool(buf.complete_while_typing()) is False

    app._picker.active = False
    assert bool(buf.complete_while_typing()) is True


def test_hint_empty_while_picker_active(app_buf):
    # The /model picker borrows the input buffer; while it's open the normal
    # "⏎ send" hint row would be misleading (the picker renders its own hint).
    app, _ = app_buf
    hint_window = app.layout.container.content.children[-1]
    assert list(hint_window.content.text())  # non-empty when idle

    app._picker.active = True
    assert list(hint_window.content.text()) == []

    app._picker.active = False
    assert list(hint_window.content.text())  # restored once picker closes
