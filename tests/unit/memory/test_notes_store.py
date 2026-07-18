"""C1 — curated_memory promoted to a first-class notes substrate (A-MEM-lite).

Additive column migration on the existing curated_memory table (title/tags/
links/source/timestamps/access_count/status/created_by), note verbs, and
[[wikilink]] parsing at write. Legacy curated_add/read/remove keep working
byte-compatibly on top (their tests live in test_curated_memory.py).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from core.sqlite_util import execute_retry
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider, parse_wikilinks

USER = "alice"


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.delenv("MEMORY_TOOL_MAX_ENTRIES", raising=False)
    monkeypatch.delenv("MEMORY_TOOL_MAX_CHARS", raising=False)
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


def test_parse_wikilinks():
    assert parse_wikilinks("see [[deploy runbook]] and [[api-keys]]") == [
        "deploy runbook", "api-keys"]
    assert parse_wikilinks("no links here") == []
    assert parse_wikilinks(None) == []


def test_migration_adds_columns_to_legacy_table(tmp_path, monkeypatch):
    """A pre-C1 curated_memory table (id, user_id, content) is widened in place."""
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    db = str(tmp_path / "memory.db")
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE curated_memory ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, content TEXT)")
    conn.execute("INSERT INTO curated_memory (user_id, content) VALUES ('alice', 'old note')")
    conn.commit(); conn.close()
    p = SqliteMemoryProvider(db)
    cols = {r["name"] for r in execute_retry(db, "PRAGMA table_info(curated_memory)", fetch="all")}
    for c in ("title", "tags", "links", "source", "created_ts", "updated_ts",
              "access_count", "status", "created_by"):
        assert c in cols
    # legacy row still readable as an active note
    out = asyncio.run(p.curated_read("alice"))
    assert "old note" in out


def test_note_create_and_get(provider):
    nid = asyncio.run(provider.note_create(
        USER, "Deploy via [[deploy runbook]]; never rsync whole tree.",
        title="prod deploys", tags=["ops", "deploy"], source="session:s1"))
    assert isinstance(nid, int)
    note = asyncio.run(provider.note_get(USER, nid))
    assert note["title"] == "prod deploys"
    assert note["tags"] == ["ops", "deploy"]
    assert note["links"] == ["deploy runbook"]
    assert note["status"] == "active"
    assert note["created_by"] == "agent"
    assert abs(note["created_ts"] - time.time()) < 60


def test_note_get_bumps_access_count(provider):
    nid = asyncio.run(provider.note_create(USER, "fact", title="t"))
    asyncio.run(provider.note_get(USER, nid))
    note = asyncio.run(provider.note_get(USER, nid))
    assert note["access_count"] == 2


def test_note_get_passive_read_does_not_bump(provider):
    """The read-only webview passes bump_access=False — browsing the wiki must
    not mint the agent-reuse signal the staleness curator keys on."""
    nid = asyncio.run(provider.note_create(USER, "fact", title="t"))
    asyncio.run(provider.note_get(USER, nid, bump_access=False))
    note = asyncio.run(provider.note_get(USER, nid, bump_access=False))
    assert note["access_count"] == 0


def test_note_update_recomputes_links(provider):
    nid = asyncio.run(provider.note_create(USER, "see [[old target]]", title="t"))
    ok = asyncio.run(provider.note_update(USER, nid, content="now see [[new target]]"))
    assert ok
    note = asyncio.run(provider.note_get(USER, nid))
    assert note["links"] == ["new target"]
    assert note["updated_ts"] >= note["created_ts"]


def test_note_archive_and_list_filter(provider):
    keep = asyncio.run(provider.note_create(USER, "keep", title="keep"))
    gone = asyncio.run(provider.note_create(USER, "gone", title="gone"))
    assert asyncio.run(provider.note_archive(USER, gone))
    active = asyncio.run(provider.note_list(USER))
    assert [n["id"] for n in active] == [keep]
    archived = asyncio.run(provider.note_list(USER, status="archived"))
    assert [n["id"] for n in archived] == [gone]


def test_note_backlinks(provider):
    asyncio.run(provider.note_create(USER, "prod tips: [[prod deploys]]", title="tips"))
    asyncio.run(provider.note_create(USER, "unrelated", title="other"))
    backs = asyncio.run(provider.note_backlinks(USER, "prod deploys"))
    assert len(backs) == 1 and backs[0]["title"] == "tips"


def test_note_tenant_isolation(provider):
    nid = asyncio.run(provider.note_create(USER, "alice note", title="a"))
    assert asyncio.run(provider.note_get("bob", nid)) is None
    assert asyncio.run(provider.note_update("bob", nid, content="hijack")) is False
    assert asyncio.run(provider.note_archive("bob", nid)) is False
    assert asyncio.run(provider.note_list("bob")) == []


def test_note_caps_still_apply(provider, monkeypatch):
    monkeypatch.setenv("MEMORY_TOOL_MAX_ENTRIES", "2")
    monkeypatch.setenv("MEMORY_TOOL_MAX_CHARS", "20")
    assert asyncio.run(provider.note_create(USER, "x" * 30, title="big")) is None
    assert asyncio.run(provider.note_create(USER, "one", title="1")) is not None
    assert asyncio.run(provider.note_create(USER, "two", title="2")) is not None
    assert asyncio.run(provider.note_create(USER, "three", title="3")) is None
    # archived notes free cap space
    first = asyncio.run(provider.note_list(USER))[0]["id"]
    asyncio.run(provider.note_archive(USER, first))
    assert asyncio.run(provider.note_create(USER, "four", title="4")) is not None


def test_pending_status_supported(provider):
    nid = asyncio.run(provider.note_create(
        USER, "from a forged turn", title="p", status="pending",
        created_by="background_review"))
    pending = asyncio.run(provider.note_list(USER, status="pending"))
    assert [n["id"] for n in pending] == [nid]
    assert asyncio.run(provider.note_list(USER)) == []  # not active


def test_anon_refused(provider):
    assert asyncio.run(provider.note_create("", "no tenant", title="x")) is None
    assert asyncio.run(provider.note_list("")) == []
