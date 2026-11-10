"""An automatic recall injects no weak match; nothing is an allowed answer."""
import asyncio

import pytest

from modules.memory.local_vector_memory_provider import LocalVectorMemoryProvider
from modules.memory.recall_rank import RECALL_WEAK_MIN_ROWS, RECALL_WEAK_SCORE, weak_match_sql
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

USER = "tenant-a"


def _seed(provider, rows: int) -> None:
    for i in range(rows):
        provider._sync_turn_blocking(USER, f"old-{i}", f"common filler row number {i}")
    provider._sync_turn_blocking(USER, "old-rare", "common zebra gadget report")


def _seed_kb(provider, rows: int) -> None:
    chunks = [f"common filler chunk {i}" for i in range(rows)] + ["common zebra gadget chunk"]
    assert asyncio.run(provider.kb_replace_source(
        user_id=USER, collection="docs", source_path="/docs/a.md", source_hash="h1", chunks=chunks))


def _prefetch(provider, query: str) -> str:
    return asyncio.run(provider.prefetch(query, session_id="now", user_id=USER))


@pytest.fixture(params=[SqliteMemoryProvider, LocalVectorMemoryProvider], ids=["sqlite", "local_vector"])
def provider(request, tmp_path):
    return request.param(str(tmp_path / "memory.db"))


def test_a_small_table_has_no_floor_and_a_large_table_has_one():
    assert weak_match_sql(RECALL_WEAK_MIN_ROWS - 1) == ("", ())
    assert weak_match_sql(RECALL_WEAK_MIN_ROWS) == (" AND rank <= ?", (-RECALL_WEAK_SCORE,))


def test_a_weak_match_injects_nothing_and_a_strong_match_stays(provider):
    _seed(provider, RECALL_WEAK_MIN_ROWS)
    assert _prefetch(provider, "common") == ""
    assert "zebra" in _prefetch(provider, "zebra")


def test_a_small_store_keeps_every_match(provider):
    _seed(provider, 10)
    assert "common" in _prefetch(provider, "common")


def test_an_explicit_search_has_no_floor(provider):
    _seed(provider, RECALL_WEAK_MIN_ROWS)
    assert "common" in asyncio.run(provider.search("common", user_id=USER, session_id="now"))


def test_the_kb_floor_is_on_the_automatic_path_only(provider):
    _seed_kb(provider, RECALL_WEAK_MIN_ROWS)
    assert asyncio.run(provider.kb_search("common", user_id=USER, stamp=False)) == ""
    assert "zebra" in asyncio.run(provider.kb_search("zebra", user_id=USER, stamp=False))
    assert "common" in asyncio.run(provider.kb_search("common", user_id=USER))
