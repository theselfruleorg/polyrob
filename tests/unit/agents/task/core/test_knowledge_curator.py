"""C4 — mechanical note consolidation on the curator tick.

LLM-free by design: (i) archive agent-authored notes never read within the
stale window; (ii) collapse exact-duplicate notes (keep the oldest). Owner-
authored notes are never touched; everything is archive-only (recoverable)
and audited via self_modification events. Gated KNOWLEDGE_CURATOR_ENABLED.
"""
from __future__ import annotations

import time

import pytest

import modules.memory.registry as reg
from agents.task.agent.core.curator import SkillCurator
from core.sqlite_util import execute_retry
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

USER = "owner-1"
OLD_TS = int(time.time()) - 120 * 86400


class _Usage:
    def list_authored(self, **kw):
        return []

    def get_state(self, key):
        return None

    def set_state(self, key, value):
        pass


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.setenv("KNOWLEDGE_CURATOR_ENABLED", "true")
    monkeypatch.delenv("KNOWLEDGE_NOTE_STALE_DAYS", raising=False)
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    reg.reset_memory_registry()
    reg.set_external_memory_provider(p)
    yield p
    reg.reset_memory_registry()


def _age(provider, note_id, ts):
    execute_retry(provider.db_path,
                  "UPDATE curated_memory SET created_ts = ?, updated_ts = ? WHERE id = ?",
                  (ts, ts, note_id))


@pytest.mark.asyncio
async def test_stale_unread_agent_note_is_archived(provider):
    stale = await provider.note_create(USER, "never read", title="stale",
                                       created_by="agent")
    fresh = await provider.note_create(USER, "fresh", title="fresh",
                                       created_by="agent")
    _age(provider, stale, OLD_TS)
    await SkillCurator(object(), _Usage()).run_once()
    active_ids = [n["id"] for n in await provider.note_list(USER)]
    assert fresh in active_ids and stale not in active_ids
    archived = await provider.note_list(USER, status="archived")
    assert [n["id"] for n in archived] == [stale]


@pytest.mark.asyncio
async def test_read_note_survives_staleness(provider):
    nid = await provider.note_create(USER, "read often", title="hot",
                                     created_by="agent")
    _age(provider, nid, OLD_TS)
    await provider.note_get(USER, nid)  # bump access_count
    await SkillCurator(object(), _Usage()).run_once()
    assert [n["id"] for n in await provider.note_list(USER)] == [nid]


@pytest.mark.asyncio
async def test_owner_notes_never_touched(provider):
    nid = await provider.note_create(USER, "owner wrote this", title="mine",
                                     created_by="user")
    _age(provider, nid, OLD_TS)
    await SkillCurator(object(), _Usage()).run_once()
    assert [n["id"] for n in await provider.note_list(USER)] == [nid]


@pytest.mark.asyncio
async def test_exact_duplicates_collapse_keep_oldest(provider):
    a = await provider.note_create(USER, "same body", title="dup-a", created_by="agent")
    b = await provider.note_create(USER, "same body", title="dup-b", created_by="agent")
    await SkillCurator(object(), _Usage()).run_once()
    active = [n["id"] for n in await provider.note_list(USER)]
    assert a in active and b not in active


@pytest.mark.asyncio
async def test_gate_off_is_noop(provider, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_CURATOR_ENABLED", "false")
    stale = await provider.note_create(USER, "never read", title="stale",
                                       created_by="agent")
    _age(provider, stale, OLD_TS)
    await SkillCurator(object(), _Usage()).run_once()
    assert [n["id"] for n in await provider.note_list(USER)] == [stale]
