import asyncio
import sqlite3

import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
from modules.memory.provider import EpisodeRecord


@pytest.fixture
def provider(tmp_path, monkeypatch):
    from modules.memory import storage_limits
    monkeypatch.setattr(storage_limits, 'MAX_ROWS', 2)
    monkeypatch.setattr(storage_limits, 'MAX_MEMORY_ROWS', 2)
    monkeypatch.setattr(storage_limits, 'MAX_BYTES', 80)
    monkeypatch.setattr(storage_limits, 'MAX_ROW_BYTES', 64)
    return SqliteMemoryProvider(str(tmp_path / 'memory.db'))


@pytest.mark.asyncio
async def test_concurrent_memory_writes_cannot_overrun_quota(provider):
    outcomes = await asyncio.gather(*[
        provider.sync_turn('', f'entry {i}', session_id=f's{i}', user_id='u')
        for i in range(8)
    ], return_exceptions=True)
    assert sum(result is True for result in outcomes) == 2
    assert await provider.sync_turn('', 'another tenant', session_id='v', user_id='v')


@pytest.mark.asyncio
async def test_failed_kb_replacement_preserves_previous_source(provider):
    kwargs = dict(user_id='u', collection='c', source_path='a', source_hash='old')
    assert await provider.kb_replace_source(**kwargs, chunks=['old'])
    assert not await provider.kb_replace_source(**kwargs, chunks=['a', 'b', 'c'])
    with sqlite3.connect(provider.db_path) as db:
        assert db.execute('SELECT content FROM kb_chunks').fetchall() == [('old',)]
    assert await provider.kb_replace_source(**kwargs, chunks=['new', 'two'])


@pytest.mark.asyncio
async def test_byte_quota_uses_utf8_bytes(provider):
    kwargs = dict(user_id='u', collection='c', source_path='a', source_hash='new')
    assert not await provider.kb_replace_source(**kwargs, chunks=['界' * 22])
    assert not await provider.kb_replace_source(**kwargs, chunks=['a' * 45, 'b' * 45])
    assert await provider.kb_replace_source(**kwargs, chunks=['ok'])


@pytest.mark.asyncio
async def test_archived_notes_still_consume_capacity(provider):
    assert await provider.note_create('u', 'one', status='archived')
    assert await provider.note_create('u', 'two', status='archived')
    assert await provider.note_create('u', 'three') is None
    assert await provider.note_create('v', 'other')


@pytest.mark.asyncio
async def test_episode_upsert_remains_possible_at_the_cap(provider):
    def record(sid, text):
        return EpisodeRecord(ts=1, user_id='u', session_id=sid, kind='chat',
                             task='task', outcome='done', summary=text, artifacts=[])
    for sid in ['a', 'b', 'c']:
        await provider.record_episode(record(sid, sid), session_id=sid, user_id='u')
    await provider.record_episode(record('a', 'updated'), session_id='a', user_id='u')
    with sqlite3.connect(provider.db_path) as db:
        assert db.execute('SELECT session_id, summary FROM episodes ORDER BY session_id').fetchall() == [('a', 'updated'), ('b', 'b')]


@pytest.mark.asyncio
async def test_turn_sync_keeps_writing_past_the_kb_row_bound(tmp_path, monkeypatch):
    """Prod 2026-10-08: 24k narration rows (8.9 MB) hit the shared 10k-row bound
    and every turn sync was refused. The memories store has its own, larger row
    bound; the byte bound and the other stores' row bound are unchanged."""
    from modules.memory import storage_limits
    monkeypatch.setattr(storage_limits, 'MAX_ROWS', 2)
    monkeypatch.setattr(storage_limits, 'MAX_MEMORY_ROWS', 5)
    provider = SqliteMemoryProvider(str(tmp_path / 'memory.db'))
    for i in range(5):
        assert await provider.sync_turn('', f'turn {i}', session_id=f's{i}', user_id='u')
    # the store is still bounded for a tenant that floods it
    with pytest.raises(ValueError):
        await provider.sync_turn('', 'turn 5', session_id='s5', user_id='u')
    # other stores keep the original row bound
    kwargs = dict(user_id='u', collection='c', source_path='a', source_hash='h')
    assert not await provider.kb_replace_source(**kwargs, chunks=['a', 'b', 'c'])
