"""Real SQLite rollback, concurrent publication and stale-vector regression."""
import asyncio
import sqlite3

import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


@pytest.fixture
def provider(tmp_path):
    return SqliteMemoryProvider(str(tmp_path / 'knowledge.db'))


async def replace(provider, chunks, source_hash='new', user='owner', collection='docs'):
    return await provider.kb_replace_source(
        user_id=user, collection=collection, source_path='source.txt',
        source_hash=source_hash, chunks=chunks,
    )


def snapshot(provider, user='owner', collection='docs'):
    with sqlite3.connect(provider.db_path) as conn:
        chunks = conn.execute(
            'SELECT content FROM kb_chunks WHERE user_id=? AND collection=? ORDER BY CAST(chunk_idx AS INTEGER)',
            (user, collection),
        ).fetchall()
        source = conn.execute(
            'SELECT source_hash, chunk_count FROM kb_sources WHERE user_id=? AND collection=?',
            (user, collection),
        ).fetchall()
    return chunks, source


@pytest.mark.asyncio
async def test_failed_metadata_write_rolls_back_deleted_and_inserted_chunks(provider):
    assert await replace(provider, ['last good', 'second chunk'], 'old')
    with sqlite3.connect(provider.db_path) as conn:
        conn.execute("CREATE TRIGGER refuse BEFORE UPDATE ON kb_sources BEGIN SELECT RAISE(ABORT, 'disk fault fixture'); END")
    assert not await replace(provider, ['replacement one', 'replacement two'])
    assert snapshot(provider) == ([('last good',), ('second chunk',)], [('old', 2)])


@pytest.mark.asyncio
async def test_two_provider_instances_publish_complete_generations(provider):
    other = SqliteMemoryProvider(provider.db_path)
    first, second = ['alpha'] * 37, ['beta'] * 19
    assert all(await asyncio.gather(replace(provider, first, 'a'), replace(other, second, 'b')))
    chunks, source = snapshot(provider)
    expected = first if source == [('a', 37)] else second
    assert source in ([('a', 37)], [('b', 19)])
    assert chunks == [(text,) for text in expected]


@pytest.mark.asyncio
async def test_replacement_scoped_and_empty_refused(provider):
    assert await replace(provider, ['owner original'], 'old')
    assert await replace(provider, ['other tenant'], user='other')
    assert await replace(provider, ['other collection'], collection='other')
    assert not await replace(provider, [])
    assert not await replace(provider, [''])
    assert not await replace(provider, ['x'], user='')
    assert snapshot(provider) == ([('owner original',)], [('old', 1)])
    assert await replace(provider, ['owner new'])
    assert snapshot(provider, user='other')[0] == [('other tenant',)]
    assert snapshot(provider, collection='other')[0] == [('other collection',)]


@pytest.mark.asyncio
async def test_registry_refuses_nontransactional_provider(monkeypatch):
    from modules.memory import registry
    from modules.memory.provider import NullMemoryProvider
    from unittest.mock import Mock
    monkeypatch.setattr(registry, 'get_memory_registry', lambda: Mock(active=lambda: NullMemoryProvider()))
    assert not await registry.kb_replace_source(
        user_id='owner', collection='docs', source_path='a', source_hash='hash', chunks=['text'])


@pytest.mark.asyncio
async def test_remove_failure_rolls_back_chunks(provider):
    assert await replace(provider, ['keep after failed remove'], 'old')
    with sqlite3.connect(provider.db_path) as conn:
        conn.execute("CREATE TRIGGER refuse BEFORE DELETE ON kb_sources BEGIN SELECT RAISE(ABORT, 'fault'); END")
    assert await provider.kb_remove(user_id='owner', collection='docs', source='source.txt') == 0
    assert snapshot(provider) == ([('keep after failed remove',)], [('old', 1)])


@pytest.mark.asyncio
async def test_append_failure_rolls_back_chunk(provider):
    assert await replace(provider, ['keep after failed append'], 'old')
    with sqlite3.connect(provider.db_path) as conn:
        conn.execute("CREATE TRIGGER refuse BEFORE UPDATE ON kb_sources BEGIN SELECT RAISE(ABORT, 'fault'); END")
    assert not await provider.kb_ingest_chunk(
        user_id='owner', collection='docs', source_path='source.txt',
        source_hash='new', chunk_idx=1, content='failed append')
    assert snapshot(provider) == ([('keep after failed append',)], [('old', 1)])


@pytest.mark.asyncio
async def test_process_death_during_replacement_keeps_old_source(provider):
    import subprocess
    import sys
    assert await replace(provider, ['survives worker crash'], 'old')
    # Exercise SQLite crash rollback after the replacement's delete/insert
    # sequence, before publishing metadata or closing the connection.
    script = '''
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('BEGIN IMMEDIATE')
conn.execute('DELETE FROM kb_chunks WHERE user_id=?', ('owner',))
conn.execute('INSERT INTO kb_chunks VALUES (?, ?, ?, ?, ?)', ('owner', 'docs', 'source.txt', '0', 'partial'))
import os
os._exit(71)
'''
    result = await asyncio.to_thread(subprocess.run, [sys.executable, '-I', '-c', script, provider.db_path],
                                    env={}, capture_output=True, timeout=5)
    assert result.returncode == 71
    assert snapshot(provider) == ([('survives worker crash',)], [('old', 1)])


@pytest.mark.asyncio
async def test_failed_vector_cleanup_cannot_recall_old_generation(tmp_path, monkeypatch):
    from modules.memory.local_vector_memory_provider import LocalVectorMemoryProvider, _vec_available
    if not _vec_available():
        pytest.skip('vector extras unavailable')
    class Embedder:
        def encode(self, text):
            return [1.0, 0.0, 0.0, 0.0]
    provider = LocalVectorMemoryProvider(str(tmp_path / 'vector.db'), embedding_model=Embedder())
    assert await replace(provider, ['obsolete postgres policy'], 'old')
    def fail(*args):
        raise OSError('cleanup fault fixture')
    monkeypatch.setattr(provider, '_kb_vec_remove', fail)
    assert await replace(provider, ['current policy'])
    # Inspect the semantic candidates directly: the old vector still exists,
    # but its content no longer matches the authoritative source transaction.
    assert provider._kb_vector_contents('anything', 'owner', 'docs', 8) == []
    assert 'current policy' in await provider.kb_search('', user_id='owner', collection='docs')
