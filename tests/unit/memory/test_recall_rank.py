"""Recall ranking (2026-10-13 review D2, A3): reader groups, recency, supersession."""
import asyncio
import time

import pytest

from core.sqlite_util import execute_retry
from modules.memory import recall_rank as rr
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

NOW = 1_800_000_000
DAY = 86400


def _row(content, *, rank=-5.0, age_days=0.0, own=False, auto=False):
    return {"content": content, "rank": rank, "ts": int(NOW - age_days * DAY),
            "own": own, "auto": auto}


def test_recency_weight_falls_to_a_floor():
    assert rr.recency_weight(NOW, now=NOW) == pytest.approx(1.0)
    assert rr.recency_weight(NOW - 14 * DAY, now=NOW) == pytest.approx(0.65)
    assert rr.recency_weight(NOW - 3650 * DAY, now=NOW) == pytest.approx(rr.RECENCY_FLOOR, abs=1e-3)
    assert rr.recency_weight(None, now=NOW) < rr.recency_weight(NOW - 30 * DAY, now=NOW)


def test_newer_row_wins_at_equal_relevance():
    old = _row("Buybacks stopped by the owner on the fifth", age_days=8)
    new = _row("Announce every buyback on X and the den", age_days=2)
    out = rr.rerank([old, new], limit=2, now=NOW)
    assert out[0] is new


def test_much_stronger_relevance_still_beats_recency():
    old = _row("old but exactly on topic", rank=-20.0, age_days=30)
    new = _row("new and barely related", rank=-2.0, age_days=0)
    assert rr.rerank([new, old], limit=1, now=NOW)[0] is old


def test_supersession_keeps_only_the_newer_near_duplicate():
    old = _row("Treasury wallet balance on base is 0.5 ETH after the bridge", rank=-9.0, age_days=5)
    new = _row("Treasury wallet balance on base is 0.7 ETH after the bridge", rank=-3.0, age_days=0)
    other = _row("Owner prefers short posts", rank=-4.0)
    out = rr.rerank([old, new, other], limit=5, now=NOW)
    assert new in out and old not in out and other in out
    assert out.index(new) == 0          # took the better slot of the pair


def test_groups_own_first_then_demotion_only_when_asked():
    chat = _row("chat row about the release", rank=-9.0)
    auto = _row("another job's release row", rank=-9.5, auto=True)
    own = _row("my job's release row", rank=-1.0, auto=True, own=True)
    assert rr.rerank([chat, auto, own], limit=3, now=NOW)[0] is own
    demoted = rr.rerank([chat, auto], limit=2, demote_automation=True, now=NOW)
    assert demoted[0] is chat
    plain = rr.rerank([chat, auto], limit=2, demote_automation=False, now=NOW)
    assert plain[0] is auto


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    return SqliteMemoryProvider(str(tmp_path / "memory.db"), top_k=5)


def _age(provider, content, days):
    execute_retry(provider.db_path,
                  "UPDATE mem_provenance SET ts = ? WHERE mem_rowid = "
                  "(SELECT rowid FROM memories WHERE content = ?)",
                  (int(time.time() - days * DAY), content))


def test_provider_prefers_the_newer_contrary_row(provider):
    """A3: of two equally matching rows, the 10-11 one beats the 10-05 one."""
    old = "Buybacks stopped, job cancelled, no buyback announcement."
    new = "Owner rule: announce every buyback on X and the den."
    for c in (old, new):
        asyncio.run(provider.sync_turn("q", c, session_id="s1", user_id="u"))
    _age(provider, old, 8)
    _age(provider, new, 2)
    out = asyncio.run(provider.search("buyback announce", user_id="u"))
    lines = out.splitlines()
    assert new in lines[0] and old in lines[1]
    assert lines[0].startswith("- [")       # dated


def test_d7_non_latin_query_recalls(provider):
    """D7: the ASCII-only tokenizer gave a Cyrillic owner turn no query at all."""
    asyncio.run(provider.sync_turn("q", "Мост на Base прибыл, 0.5 ETH.", session_id="s1",
                                   user_id="u"))
    out = asyncio.run(provider.prefetch("мост прибыл?", session_id="other", user_id="u"))
    assert "Мост на Base" in out
