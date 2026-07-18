"""Tests for the ``/self`` REPL slash-command handler (cli/ui/commands/h_self.py).

Hermetic: the ``core.instance`` loaders are monkeypatched (on the module the
handler imports them from) to return canned identity docs — nothing touches the
real ``<home>/identity/`` tree.
"""

from __future__ import annotations

import io

import pytest

from cli.ui.commands.registry import CommandContext
from cli.ui.commands.h_self import h_self, _preview, _DOC_PREVIEW_CHARS, _TRUNCATION_MARKER
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


# ---------------------------------------------------------------------------
# Stub context helper (mirrors tests/unit/cli/ui/test_commands.py::_plain_ctx)
# ---------------------------------------------------------------------------


def _plain_ctx(**overrides):
    """Build a CommandContext with a PlainRenderer writing to a StringIO."""
    buf = io.StringIO()
    state = overrides.pop("state", SessionState())
    renderer = PlainRenderer(state=state, stream=buf)
    ctx = CommandContext(renderer=renderer, state=state, **overrides)
    return ctx, buf


def _patch_instance(monkeypatch, *, instance="rob", owner=None, soul="", self_doc=""):
    """Patch the core.instance loaders as imported by the handler module."""
    # The handler does a local ``from core.instance import ...`` at call time, so
    # patching the origin module (core.instance) is what takes effect.
    monkeypatch.setattr("core.instance.resolve_instance_id", lambda *a, **k: instance)
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: owner)
    monkeypatch.setattr("core.instance.load_self_context", lambda home: soul)
    monkeypatch.setattr("core.instance.load_self_doc", lambda home, uid, iid: self_doc)


# ---------------------------------------------------------------------------
# _preview truncation unit
# ---------------------------------------------------------------------------


def test_preview_passthrough_when_short():
    assert _preview("hello world") == "hello world"
    assert _TRUNCATION_MARKER not in _preview("short")


def test_preview_truncates_long_text():
    long = "x" * (_DOC_PREVIEW_CHARS + 500)
    out = _preview(long)
    assert _TRUNCATION_MARKER in out
    assert len(out) < len(long)


# ---------------------------------------------------------------------------
# /self — happy path
# ---------------------------------------------------------------------------


def test_self_shows_identity_and_docs(monkeypatch):
    _patch_instance(
        monkeypatch,
        instance="rob",
        owner="owner-42",
        soul="I am the SOUL of this instance.",
        self_doc="This is my evolving SELF note.",
    )
    ctx, buf = _plain_ctx(user_id="alice")
    h_self(ctx)
    out = buf.getvalue()
    assert "rob" in out                       # instance id
    assert "owner-42" in out                  # bound owner
    assert "alice" in out                     # user id
    assert "SOUL of this instance" in out     # SOUL snippet
    assert "evolving SELF note" in out        # SELF snippet


def test_self_unbound_owner_placeholder(monkeypatch):
    _patch_instance(monkeypatch, owner=None, soul="soul text", self_doc="self text")
    ctx, buf = _plain_ctx()
    h_self(ctx)
    out = buf.getvalue()
    assert "unbound (local owner)" in out


def test_self_truncates_long_soul(monkeypatch):
    big_soul = "S" * (_DOC_PREVIEW_CHARS + 1000)
    _patch_instance(monkeypatch, soul=big_soul, self_doc="")
    ctx, buf = _plain_ctx()
    h_self(ctx)
    out = buf.getvalue()
    assert _TRUNCATION_MARKER in out
    # The full doc must NOT be emitted verbatim.
    assert big_soul not in out


# ---------------------------------------------------------------------------
# /self — empty docs are graceful
# ---------------------------------------------------------------------------


def test_self_empty_docs_graceful(monkeypatch):
    _patch_instance(monkeypatch, soul="", self_doc="")
    ctx, buf = _plain_ctx()
    h_self(ctx)
    out = buf.getvalue()
    assert "no SOUL/identity doc authored" in out
    assert "no SELF/identity doc authored" in out


# ---------------------------------------------------------------------------
# /self — fail-open: a raising loader must not raise into the REPL
# ---------------------------------------------------------------------------


def test_self_soul_loader_raises_is_failopen(monkeypatch):
    def _boom(home):
        raise RuntimeError("disk gone")

    _patch_instance(monkeypatch, owner="o1", soul="", self_doc="ok self")
    monkeypatch.setattr("core.instance.load_self_context", _boom)
    ctx, buf = _plain_ctx()
    # Must not raise.
    h_self(ctx)
    out = buf.getvalue()
    # SOUL degrades to the empty placeholder; SELF still shows.
    assert "no SOUL/identity doc authored" in out
    assert "ok self" in out


def test_self_top_level_failure_is_failopen(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("kaput")

    # resolve_instance_id raising blows the whole try — must be caught.
    monkeypatch.setattr("core.instance.resolve_instance_id", _boom)
    ctx, buf = _plain_ctx()
    h_self(ctx)  # must not raise
    assert "Could not resolve self-context" in buf.getvalue()


def test_self_home_dir_mirrors_container_config(monkeypatch):
    """home_dir passed to the loaders is the container config's data_dir (the
    construction.py pattern), not a hardcoded path."""
    seen = {}

    monkeypatch.setattr("core.instance.resolve_instance_id", lambda *a, **k: "rob")
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: None)
    monkeypatch.setattr(
        "core.instance.load_self_context",
        lambda home: seen.setdefault("soul_home", home) or "",
    )
    monkeypatch.setattr(
        "core.instance.load_self_doc",
        lambda home, uid, iid: seen.setdefault("self_home", home) or "",
    )

    from types import SimpleNamespace

    container = SimpleNamespace(config=SimpleNamespace(data_dir="/tmp/polyrob-data"))
    ctx, buf = _plain_ctx(container=container)
    h_self(ctx)
    assert seen["soul_home"] == "/tmp/polyrob-data"
    assert seen["self_home"] == "/tmp/polyrob-data"
