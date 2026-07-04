"""Phase 5: renderer-selection / plain-fallback matrix.

Centralized fallback (proposal §7.4): non-TTY stdout OR ``NO_COLOR`` OR
``TERM=dumb`` OR ``--plain`` -> PlainRenderer.  Otherwise RichRenderer.
"""
import io

import pytest

from cli.ui import select_renderer, use_rich
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.rich_renderer import RichRenderer
from cli.ui.state import SessionState


class _TTYStream(io.StringIO):
    """A StringIO that claims to be a TTY."""

    def isatty(self):  # noqa: D401
        return True


class _NonTTYStream(io.StringIO):
    def isatty(self):
        return False


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start each case from a clean color env."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    yield


def test_tty_no_flags_uses_rich(monkeypatch):
    assert use_rich(plain=False, stream=_TTYStream()) is True
    r = select_renderer(SessionState(), plain=False, stream=_TTYStream())
    assert isinstance(r, RichRenderer)


def test_non_tty_falls_back_to_plain():
    assert use_rich(plain=False, stream=_NonTTYStream()) is False
    r = select_renderer(SessionState(), plain=False, stream=_NonTTYStream())
    assert isinstance(r, PlainRenderer)


def test_plain_flag_forces_plain_even_on_tty():
    assert use_rich(plain=True, stream=_TTYStream()) is False
    r = select_renderer(SessionState(), plain=True, stream=_TTYStream())
    assert isinstance(r, PlainRenderer)


def test_no_color_forces_plain_on_tty(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert use_rich(plain=False, stream=_TTYStream()) is False
    r = select_renderer(SessionState(), plain=False, stream=_TTYStream())
    assert isinstance(r, PlainRenderer)


def test_term_dumb_forces_plain_on_tty(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    assert use_rich(plain=False, stream=_TTYStream()) is False
    r = select_renderer(SessionState(), plain=False, stream=_TTYStream())
    assert isinstance(r, PlainRenderer)


def test_one_shot_flag_threads_into_rich_renderer():
    r = select_renderer(SessionState(), plain=False, stream=_TTYStream(), one_shot=True)
    assert isinstance(r, RichRenderer)
    assert r._one_shot is True
