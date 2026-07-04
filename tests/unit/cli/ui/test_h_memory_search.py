"""Tests for ``/memory search`` (cross-session recall) in the REPL command handler.

Covers the async ``_h_memory`` handler:
- ``/memory search <query>`` emits recall hits from the active provider.
- bare ``/memory`` still shows the active provider name (legacy behavior).
- graceful no-results ("No matches.") and no-provider messages.
- empty query → usage hint.

Hermetic: monkeypatches ``modules.memory.registry.get_memory_registry`` to
return a fake provider whose async ``search`` returns canned hits — no real DB,
no real registry.
"""
from __future__ import annotations

import asyncio
import io

import pytest

from cli.ui.commands import CommandContext
from cli.ui.commands.handlers import _h_memory
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plain_ctx(**overrides):
    """Build a CommandContext writing renderer output to a StringIO (mirrors
    tests/unit/cli/ui/test_commands.py::_plain_ctx)."""
    buf = io.StringIO()
    state = overrides.pop("state", SessionState())
    renderer = PlainRenderer(state=state, stream=buf)
    ctx = CommandContext(renderer=renderer, state=state, **overrides)
    return ctx, buf


class _FakeProvider:
    """Stand-in for an external MemoryProvider with an async search()."""

    def __init__(self, *, name="fake-fts", is_external=True, result="", search=True):
        self.name = name
        self.is_external = is_external
        self._result = result
        self.calls = []
        if not search:
            # Drop the search attribute entirely so callable(getattr(...)) is False.
            self.search = None

    async def search(self, query, *, user_id=None, session_id=None, limit=5, sort=None):
        self.calls.append({"query": query, "user_id": user_id, "limit": limit})
        return self._result


class _FakeRegistry:
    def __init__(self, provider):
        self._provider = provider

    def active(self):
        return self._provider


def _patch_registry(monkeypatch, provider):
    monkeypatch.setattr(
        "modules.memory.registry.get_memory_registry",
        lambda: _FakeRegistry(provider),
    )
    return provider


# ---------------------------------------------------------------------------
# /memory search — hits
# ---------------------------------------------------------------------------


def test_memory_search_emits_hits(monkeypatch):
    provider = _patch_registry(
        monkeypatch,
        _FakeProvider(result="- first recalled fact\n- second recalled fact"),
    )
    ctx, buf = _plain_ctx(user_id="u1", args=["search", "deployment", "notes"])
    asyncio.run(_h_memory(ctx))
    out = buf.getvalue()
    assert "first recalled fact" in out
    assert "second recalled fact" in out
    # Query is the args after "search", joined; user_id + limit=10 forwarded.
    assert provider.calls == [{"query": "deployment notes", "user_id": "u1", "limit": 10}]


def test_memory_search_defaults_user_id_to_local(monkeypatch):
    provider = _patch_registry(monkeypatch, _FakeProvider(result="- hit"))
    # user_id falsy → defaults to "local"
    ctx, buf = _plain_ctx(user_id="", args=["search", "foo"])
    asyncio.run(_h_memory(ctx))
    assert provider.calls[0]["user_id"] == "local"


# ---------------------------------------------------------------------------
# /memory search — graceful edges
# ---------------------------------------------------------------------------


def test_memory_search_no_results(monkeypatch):
    _patch_registry(monkeypatch, _FakeProvider(result=""))
    ctx, buf = _plain_ctx(user_id="u1", args=["search", "nothing"])
    asyncio.run(_h_memory(ctx))
    assert "No matches." in buf.getvalue()


def test_memory_search_empty_query_shows_usage(monkeypatch):
    _patch_registry(monkeypatch, _FakeProvider(result="- unused"))
    ctx, buf = _plain_ctx(user_id="u1", args=["search"])
    asyncio.run(_h_memory(ctx))
    assert "Usage: /memory search" in buf.getvalue()


def test_memory_search_no_external_provider(monkeypatch):
    # Null/internal provider (is_external False) → not searchable.
    _patch_registry(monkeypatch, _FakeProvider(name="null", is_external=False))
    ctx, buf = _plain_ctx(user_id="u1", args=["search", "foo"])
    asyncio.run(_h_memory(ctx))
    assert "No searchable memory backend active" in buf.getvalue()


def test_memory_search_provider_none(monkeypatch):
    _patch_registry(monkeypatch, None)
    ctx, buf = _plain_ctx(user_id="u1", args=["search", "foo"])
    asyncio.run(_h_memory(ctx))
    assert "No searchable memory backend active" in buf.getvalue()


def test_memory_search_provider_without_search_callable(monkeypatch):
    _patch_registry(monkeypatch, _FakeProvider(result="", search=False))
    ctx, buf = _plain_ctx(user_id="u1", args=["search", "foo"])
    asyncio.run(_h_memory(ctx))
    assert "No searchable memory backend active" in buf.getvalue()


def test_memory_search_fail_open_on_registry_error(monkeypatch):
    def _boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr("modules.memory.registry.get_memory_registry", _boom)
    ctx, buf = _plain_ctx(user_id="u1", args=["search", "foo"])
    # Must not raise.
    asyncio.run(_h_memory(ctx))
    assert "Could not resolve memory provider" in buf.getvalue()


# ---------------------------------------------------------------------------
# bare /memory — legacy behavior preserved
# ---------------------------------------------------------------------------


def test_memory_bare_shows_active_provider(monkeypatch):
    _patch_registry(monkeypatch, _FakeProvider(name="sqlite-fts"))
    ctx, buf = _plain_ctx(user_id="u1", args=[])
    asyncio.run(_h_memory(ctx))
    out = buf.getvalue()
    assert "Active memory provider: sqlite-fts" in out


def test_memory_bare_no_external_backend(monkeypatch):
    _patch_registry(monkeypatch, _FakeProvider(name="null", is_external=False))
    ctx, buf = _plain_ctx(user_id="u1", args=[])
    asyncio.run(_h_memory(ctx))
    assert "No external memory backend active" in buf.getvalue()


def test_memory_first_arg_not_search_shows_provider(monkeypatch):
    # A non-"search" first arg falls through to the legacy provider-name path.
    _patch_registry(monkeypatch, _FakeProvider(name="sqlite-fts"))
    ctx, buf = _plain_ctx(user_id="u1", args=["status"])
    asyncio.run(_h_memory(ctx))
    assert "Active memory provider: sqlite-fts" in buf.getvalue()
