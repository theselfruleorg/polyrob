"""UP-09 Step 9.4 — curated per-tenant memory store on SqliteMemoryProvider.

Backs the optional `memory` tool. Tenant-isolated, char/entry-capped, anon-refused.
"""
import asyncio

import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.setenv("MEMORY_TOOL_MAX_ENTRIES", "3")
    monkeypatch.setenv("MEMORY_TOOL_MAX_CHARS", "50")
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


def test_add_read_roundtrip(provider):
    assert asyncio.run(provider.curated_add("alice", "remember the API key rotates monthly"))
    out = asyncio.run(provider.curated_read("alice"))
    assert "API key rotates" in out


def test_tenant_isolation(provider):
    asyncio.run(provider.curated_add("alice", "alice secret"))
    asyncio.run(provider.curated_add("bob", "bob secret"))
    assert "alice secret" in asyncio.run(provider.curated_read("alice"))
    assert "bob" not in asyncio.run(provider.curated_read("alice"))


def test_remove_by_substring(provider):
    asyncio.run(provider.curated_add("alice", "keep this one"))
    asyncio.run(provider.curated_add("alice", "delete this one"))
    asyncio.run(provider.curated_remove("alice", "delete this"))
    out = asyncio.run(provider.curated_read("alice"))
    assert "keep this one" in out
    assert "delete this one" not in out


def test_over_entry_cap_rejected(provider):
    for i in range(3):
        assert asyncio.run(provider.curated_add("alice", f"note {i}"))
    # 4th exceeds MEMORY_TOOL_MAX_ENTRIES=3
    assert asyncio.run(provider.curated_add("alice", "note 4")) is False


def test_over_char_cap_rejected(provider):
    assert asyncio.run(provider.curated_add("alice", "x" * 100)) is False  # > 50 chars


def test_anon_refused(provider):
    assert asyncio.run(provider.curated_add("", "no tenant")) is False
    assert asyncio.run(provider.curated_read("")) == ""
