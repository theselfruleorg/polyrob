"""Only an explicit search stamps ``last_hit_at``. The automatic prefetch kept
every written document alive: the injection refreshed the clock the age-out reads,
also for a chunk ``_budget`` then trimmed away."""
import asyncio
from types import SimpleNamespace

import pytest

from core.sqlite_util import execute_retry
from modules.memory.local_vector_memory_provider import LocalVectorMemoryProvider
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


def _ingest(provider, source="a.md", collection="written", text="quarterly widget budget"):
    asyncio.run(provider.kb_ingest_chunk(user_id="u", collection=collection, source_path=source,
                                         source_hash="h", chunk_idx=0, content=text))


def _hit(provider):
    rows = execute_retry(provider.db_path, "SELECT last_hit_at FROM kb_sources", (), fetch="all")
    return [r["last_hit_at"] for r in rows]


@pytest.fixture(params=["sqlite", "vector_degraded"])
def provider(request, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    if request.param == "sqlite":
        return SqliteMemoryProvider(str(tmp_path / "m.db"))
    prov = LocalVectorMemoryProvider(str(tmp_path / "m.db"))
    prov._vec_ok = False  # the keyword branch of the hybrid override
    return prov


def test_a_search_with_stamp_off_leaves_the_clock(provider):
    _ingest(provider)
    out = asyncio.run(provider.kb_search("widget", user_id="u", stamp=False))
    assert "widget" in out
    assert not any(_hit(provider))


def test_an_explicit_search_still_stamps(provider):
    _ingest(provider)
    assert "widget" in asyncio.run(provider.kb_search("widget", user_id="u"))
    assert all(_hit(provider))


def test_the_hybrid_merge_branch_honors_stamp_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    prov = LocalVectorMemoryProvider(str(tmp_path / "m.db"))
    _ingest(prov)
    prov._vec_ok = True
    monkeypatch.setattr(prov, "_kb_vector_contents",
                        lambda q, n, c, l: [("quarterly widget budget", "a.md", 0)])
    assert "widget" in asyncio.run(prov.kb_search("widget", user_id="u", stamp=False))
    assert not any(_hit(prov))
    assert "widget" in asyncio.run(prov.kb_search("widget", user_id="u"))
    assert all(_hit(prov))


def test_the_prefetch_never_stamps(tmp_path, monkeypatch):
    from agents.task.agent.core import memory_prefetch as mp
    from modules.memory import registry as R
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("KB_AUTO_PREFETCH", "true")
    prov = SqliteMemoryProvider(str(tmp_path / "m.db"))
    _ingest(prov)
    monkeypatch.setattr(R, "get_memory_registry", lambda: SimpleNamespace(active=lambda: prov))
    msg = asyncio.run(mp.build_prefetch_message("widget budget", session_id="s", user_id="u"))
    assert msg is not None and "widget" in str(msg.content)
    assert not any(_hit(prov))


def test_the_age_out_removes_the_index_row_and_keeps_the_file(tmp_path, monkeypatch):
    """A written document that only prefetch reached ages out of the KB. The file stays."""
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    doc = tmp_path / "doc.md"
    doc.write_text("quarterly widget budget")
    prov = SqliteMemoryProvider(str(tmp_path / "m.db"))
    _ingest(prov, source=str(doc))
    asyncio.run(prov.kb_search("widget", user_id="u", stamp=False))
    assert prov.kb_unread_sources(collection="written", before_ts=4102444800.0)
    assert asyncio.run(prov.kb_remove(user_id="u", collection="written", source=str(doc))) == 1
    assert doc.exists()


# --- One source takes the budget once across collections ------------------------

def test_a_twin_in_a_second_collection_takes_no_result_slot(provider):
    for coll in ("default", "written"):
        _ingest(provider, source="twin.md", collection=coll, text="widget rules for buying")
    for i in range(3):
        _ingest(provider, source=f"other{i}.md", collection="default", text=f"widget note {i}")
    out = asyncio.run(provider.kb_search("widget", user_id="u", limit=4))
    lines = out.splitlines()
    assert len(lines) == 4
    assert sum("twin.md" in ln for ln in lines) == 1
    assert sum("other" in ln for ln in lines) == 3


def test_an_older_snapshot_of_the_same_chunk_answers_once(provider):
    _ingest(provider, source="doc.md", collection="default", text="widget price is one")
    _ingest(provider, source="doc.md", collection="written", text="widget price is two")
    out = asyncio.run(provider.kb_search("widget", user_id="u", limit=4))
    assert len(out.splitlines()) == 1


def test_the_hybrid_merge_answers_a_chunk_once(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    prov = LocalVectorMemoryProvider(str(tmp_path / "m.db"))
    _ingest(prov, source="doc.md", collection="default", text="widget price is one")
    _ingest(prov, source="b.md", collection="default", text="widget stock is low")
    prov._vec_ok = True
    monkeypatch.setattr(prov, "_kb_vector_contents", lambda q, n, c, l: [
        ("widget price is two", "doc.md", 0), ("widget stock is low", "b.md", 0)])
    lines = asyncio.run(prov.kb_search("widget", user_id="u", limit=4)).splitlines()
    assert len(lines) == 2 and sum("doc.md" in ln for ln in lines) == 1
