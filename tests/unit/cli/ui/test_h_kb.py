"""Tests for the ``/kb`` REPL slash-command handler (cli/ui/commands/h_kb.py).

Hermetic: the memory backend / embedder is never built. We monkeypatch the
enable gate + the ``_ensure_memory_backend`` bootstrap + the registry routers
(``kb_list_sources`` / ``kb_search``) on their home modules, so no real
provider is needed.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from cli.ui.commands.h_kb import h_kb
from cli.ui.commands.registry import CommandContext
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plain_ctx(args=None, user_id="local"):
    """CommandContext writing through a PlainRenderer into a StringIO."""
    buf = io.StringIO()
    state = SessionState()
    renderer = PlainRenderer(state=state, stream=buf)
    ctx = CommandContext(
        renderer=renderer, state=state, args=list(args or []), user_id=user_id
    )
    return ctx, buf


def _patch(monkeypatch, *, enabled=True, sources=None, search_result=""):
    """Wire the gate / bootstrap / routers with hermetic stubs. Returns a spy dict."""
    spy = {"ensure_called": False, "list_kwargs": None, "search_args": None}

    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: enabled)

    async def _fake_ensure():
        spy["ensure_called"] = True

    monkeypatch.setattr("cli.commands.kb._ensure_memory_backend", _fake_ensure)

    async def _fake_list(*, user_id=None, collection=None):
        spy["list_kwargs"] = {"user_id": user_id, "collection": collection}
        return list(sources or [])

    async def _fake_search(query, *, user_id=None, collection="default", limit=8):
        spy["search_args"] = {
            "query": query, "user_id": user_id,
            "collection": collection, "limit": limit,
        }
        return search_result

    monkeypatch.setattr("modules.memory.registry.kb_list_sources", _fake_list)
    monkeypatch.setattr("modules.memory.registry.kb_search", _fake_search)
    return spy


# ---------------------------------------------------------------------------
# Disabled gate
# ---------------------------------------------------------------------------


def test_kb_disabled_is_graceful(monkeypatch):
    _patch(monkeypatch, enabled=False)
    ctx, buf = _plain_ctx()
    asyncio.run(h_kb(ctx))
    assert "disabled" in buf.getvalue().lower()


def test_kb_gate_raises_is_graceful(monkeypatch):
    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr("cli.commands.kb._kb_enabled", _boom)
    ctx, buf = _plain_ctx()
    asyncio.run(h_kb(ctx))
    out = buf.getvalue().lower()
    assert "unavailable" in out and "no config" in out


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_kb_list_default_shows_sources(monkeypatch):
    spy = _patch(monkeypatch, sources=["docs/a.md", "docs/b.md"])
    ctx, buf = _plain_ctx()  # bare /kb → list all
    asyncio.run(h_kb(ctx))
    out = buf.getvalue()
    assert spy["ensure_called"] is True
    assert "KB sources (2)" in out
    assert "docs/a.md" in out and "docs/b.md" in out
    # bare /kb lists across all collections
    assert spy["list_kwargs"] == {"user_id": "local", "collection": None}


def test_kb_list_empty_is_graceful(monkeypatch):
    _patch(monkeypatch, sources=[])
    ctx, buf = _plain_ctx(args=["list"])
    asyncio.run(h_kb(ctx))
    assert "no sources in the knowledge base" in buf.getvalue()


def test_kb_list_with_collection_filter(monkeypatch):
    spy = _patch(monkeypatch, sources=["notes/x.md"])
    ctx, buf = _plain_ctx(args=["list", "notes"])
    asyncio.run(h_kb(ctx))
    out = buf.getvalue()
    assert "notes/x.md" in out
    assert spy["list_kwargs"]["collection"] == "notes"


def test_kb_bare_collection_shorthand(monkeypatch):
    spy = _patch(monkeypatch, sources=["k.md"])
    ctx, buf = _plain_ctx(args=["mycoll"])  # /kb mycoll → list that collection
    asyncio.run(h_kb(ctx))
    assert spy["list_kwargs"]["collection"] == "mycoll"


def test_kb_list_uses_ctx_user_id(monkeypatch):
    spy = _patch(monkeypatch, sources=["k.md"])
    ctx, buf = _plain_ctx(user_id="")  # falls back to "local"
    asyncio.run(h_kb(ctx))
    assert spy["list_kwargs"]["user_id"] == "local"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_kb_search_shows_results(monkeypatch):
    spy = _patch(monkeypatch, search_result="hit one\nhit two")
    ctx, buf = _plain_ctx(args=["search", "how", "to", "deploy"])
    asyncio.run(h_kb(ctx))
    out = buf.getvalue()
    assert spy["ensure_called"] is True
    assert "hit one" in out and "hit two" in out
    # multi-word query is joined
    assert spy["search_args"]["query"] == "how to deploy"
    assert spy["search_args"]["collection"] == "default"


def test_kb_search_no_results_is_graceful(monkeypatch):
    _patch(monkeypatch, search_result="")
    ctx, buf = _plain_ctx(args=["search", "nothingmatches"])
    asyncio.run(h_kb(ctx))
    assert "no results for" in buf.getvalue()


def test_kb_search_missing_query_shows_usage(monkeypatch):
    _patch(monkeypatch, search_result="x")
    ctx, buf = _plain_ctx(args=["search"])
    asyncio.run(h_kb(ctx))
    out = buf.getvalue()
    assert "Usage" in out
    assert "/kb search" in out


# ---------------------------------------------------------------------------
# Fail-open: a raising router degrades to a one-liner, never propagates
# ---------------------------------------------------------------------------


def test_kb_list_router_error_is_graceful(monkeypatch):
    _patch(monkeypatch, sources=[])

    async def _boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr("modules.memory.registry.kb_list_sources", _boom)
    ctx, buf = _plain_ctx()
    asyncio.run(h_kb(ctx))  # must not raise
    assert "failed" in buf.getvalue().lower()


def test_kb_search_router_error_is_graceful(monkeypatch):
    _patch(monkeypatch)

    async def _boom(*a, **k):
        raise RuntimeError("embedder down")

    monkeypatch.setattr("modules.memory.registry.kb_search", _boom)
    ctx, buf = _plain_ctx(args=["search", "q"])
    asyncio.run(h_kb(ctx))  # must not raise
    assert "failed" in buf.getvalue().lower()
