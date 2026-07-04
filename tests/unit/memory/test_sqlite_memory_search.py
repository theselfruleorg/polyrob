"""UP-09 Step 9.2 — SqliteMemoryProvider.search() tenant isolation, limit, sort, browse.

prefetch() must keep its exact legacy shape (rank-ordered, top_k, "" on no-terms/anon).
"""
import asyncio
import os

import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    return SqliteMemoryProvider(str(tmp_path / "memory.db"), top_k=5)


def _seed(provider, user_id, contents, session="s1"):
    for i, c in enumerate(contents):
        asyncio.run(provider.sync_turn(c, f"reply {i}", session_id=session, user_id=user_id))


def test_search_tenant_isolation(provider):
    _seed(provider, "alice", ["alpha widget deployment"])
    _seed(provider, "bob", ["alpha widget deployment"])
    a = asyncio.run(provider.search("widget", user_id="alice"))
    b = asyncio.run(provider.search("widget", user_id="bob"))
    assert "alpha widget" in a
    assert "alpha widget" in b
    # Each only sees its own row (one match each, not two).
    assert a.count("- ") == 1
    assert b.count("- ") == 1


def test_search_respects_limit(provider):
    _seed(provider, "alice", [f"widget number {i}" for i in range(10)])
    res = asyncio.run(provider.search("widget", user_id="alice", limit=2))
    assert res.count("- ") == 2


def test_search_limit_clamped(provider):
    _seed(provider, "alice", [f"widget {i}" for i in range(30)])
    res = asyncio.run(provider.search("widget", user_id="alice", limit=999))
    assert res.count("- ") <= 20
    res0 = asyncio.run(provider.search("widget", user_id="alice", limit=0))
    assert res0.count("- ") == 1  # clamped up to 1


def test_search_sort_newest_oldest(provider):
    _seed(provider, "alice", ["widget first", "widget second", "widget third"])
    newest = asyncio.run(provider.search("widget", user_id="alice", limit=1, sort="newest"))
    oldest = asyncio.run(provider.search("widget", user_id="alice", limit=1, sort="oldest"))
    assert "third" in newest
    assert "first" in oldest


def test_browse_empty_query_recent(provider):
    _seed(provider, "alice", ["widget first", "gadget second", "gizmo third"])
    res = asyncio.run(provider.search("", user_id="alice", limit=2))
    # browse => most-recent rows regardless of keyword
    assert "third" in res
    assert "second" in res
    assert "first" not in res


def test_browse_tenant_isolation(provider):
    _seed(provider, "alice", ["alice note"])
    _seed(provider, "bob", ["bob note"])
    res = asyncio.run(provider.search("", user_id="alice"))
    assert "alice note" in res
    assert "bob note" not in res


def test_anon_blocked_returns_empty(provider):
    _seed(provider, "alice", ["secret data"])
    assert asyncio.run(provider.search("secret", user_id="")) == ""
    assert asyncio.run(provider.search("", user_id=None)) == ""


def test_anon_allowed_when_not_required(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    p = SqliteMemoryProvider(str(tmp_path / "m.db"), top_k=5)
    _seed(p, "", ["shared bucket widget"])
    assert "shared bucket" in asyncio.run(p.search("widget", user_id=""))


def test_prefetch_legacy_shape_unchanged(provider):
    _seed(provider, "alice", ["widget alpha"])
    # prefetch: rank-ordered, returns "" on no-terms, "" on anon
    assert "widget alpha" in asyncio.run(provider.prefetch("widget", session_id="s1", user_id="alice"))
    assert asyncio.run(provider.prefetch("", session_id="s1", user_id="alice")) == ""
    assert asyncio.run(provider.prefetch("a b", session_id="s1", user_id="alice")) == ""  # all <3 chars
    assert asyncio.run(provider.prefetch("widget", session_id="s1", user_id="")) == ""
