"""A rank-ordered read fuses the any-term and the all-terms query."""
import asyncio

import pytest

from modules.memory.local_vector_memory_provider import LocalVectorMemoryProvider
from modules.memory.recall_rank import fuse_ranked
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

USER = "tenant-a"


@pytest.fixture(params=[SqliteMemoryProvider, LocalVectorMemoryProvider], ids=["sqlite", "local_vector"])
def provider(request, tmp_path):
    return request.param(str(tmp_path / "memory.db"), top_k=2)


def test_a_row_with_every_term_reaches_the_top_k(provider):
    for i in range(50):
        provider._sync_turn_blocking(USER, f"z{i}", f"zebra zebra row {i}")
        provider._sync_turn_blocking(USER, f"g{i}", f"gadget gadget row {i}")
    provider._sync_turn_blocking(USER, "whole", "one long report " + "plain words " * 80 + "zebra gadget")
    any_term = provider._keyword_rows_for("zebra gadget", norm_user=USER, limit=2)
    assert not any("long report" in r["content"] for r in any_term)
    out = asyncio.run(provider.prefetch("zebra gadget", session_id="now", user_id=USER))
    assert "long report" in out


def test_a_browse_and_a_time_sort_ask_once(provider, monkeypatch):
    provider._sync_turn_blocking(USER, "s", "zebra gadget row")
    calls = []
    real = provider._keyword_rows_for
    monkeypatch.setattr(provider, "_keyword_rows_for",
                        lambda q, **kw: calls.append(kw.get("all_terms")) or real(q, **kw))
    provider._keyword_rows("", norm_user=USER, limit=2)
    provider._keyword_rows("zebra gadget", norm_user=USER, limit=2, sort="newest")
    provider._keyword_rows("zebra", norm_user=USER, limit=2)
    assert calls == [None, None, None]


def _row(rowid, content, **kw):
    return {"rowid": rowid, "content": content, **kw}


def test_the_fused_list_keeps_the_reader_group_order_and_one_of_two_twins():
    chat = _row(1, "chat row")
    auto = _row(2, "automation row", auto=True)
    assert fuse_ranked([auto, chat], [auto], limit=5)[0] is chat
    assert fuse_ranked([auto, chat], [auto], limit=5, demote_automation=False)[0] is auto
    own = _row(3, "own job row", own=True, auto=True)
    assert fuse_ranked([chat], [own], limit=5)[0] is own
    twin_a = _row(4, "alpha beta gamma delta epsilon")
    twin_b = _row(5, "alpha beta gamma delta epsilon")
    assert fuse_ranked([twin_a], [twin_b], limit=5) == [twin_a]
    assert len(fuse_ranked([chat, auto], [own], limit=2)) == 2
