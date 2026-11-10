"""An explicit search also reads the run records; no recall does."""
import asyncio
import sqlite3
import time

import pytest

from modules.memory import scope as _scope
from modules.memory.provider import EpisodeRecord
from modules.memory.run_records import best_rows, episode_docs
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
from tools.controller._helpers import run_record_section

USER, OTHER = "tenant-a", "tenant-b"


@pytest.fixture
def world(tmp_path):
    provider = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    now = time.time()
    for user, sid, text in ((USER, "r1", "zebra run record"), (OTHER, "r2", "zebra other record")):
        asyncio.run(provider.record_episode(
            EpisodeRecord(ts=int(now), user_id=user, session_id=sid, kind="cron", task="check",
                          outcome="done", summary=text), session_id=sid, user_id=user))
    return provider


def _sections(world, query="zebra", **kw):
    args = dict(provider=world, user_id=USER, session_id="now", limit=5)
    return run_record_section(query, **{**args, **kw})


def test_the_run_records_answer_for_this_tenant_only(world):
    out = _sections(world)
    assert "zebra run record" in out and "other record" not in out


@pytest.mark.parametrize("kw", [dict(query="  "), dict(sort="newest"), dict(before_id=9),
                                dict(is_sub_agent=True), dict(query="absentword")])
def test_a_browse_a_page_a_child_and_no_match_read_nothing(world, kw):
    assert _sections(world, **kw) == ""


def test_a_session_in_a_memory_scope_reads_nothing(world, monkeypatch):
    monkeypatch.setattr(_scope, "scopes_enabled", lambda: True)
    monkeypatch.setattr(_scope, "session_scope", lambda _sid: object())
    assert _sections(world) == ""


def test_a_room_regime_reads_nothing(world, monkeypatch):
    from agents.task import session_class
    monkeypatch.setattr(session_class, "session_regime", lambda *a, **k: session_class.ROOM)
    assert _sections(world) == ""


def test_an_unreadable_store_says_so(world):
    with sqlite3.connect(world.db_path) as db:
        db.execute("DROP TABLE episodes")
    assert "could not be read (OperationalError)" in _sections(world)


def test_the_unverified_rule_reaches_a_run_record(world):
    provider = world
    asyncio.run(provider.record_episode(
        EpisodeRecord(ts=int(time.time()), user_id=USER, session_id="r3", kind="goal",
                      task="", summary="[unverified] zebra guess"), session_id="r3", user_id=USER))
    joined = " ".join(d["content"] for d in episode_docs(provider.db_path, USER, drop_unverified=True))
    assert "zebra guess" not in joined
    assert "zebra guess" in " ".join(d["content"] for d in episode_docs(provider.db_path, USER))


def test_best_rows_ranks_and_keeps_the_newer_of_two_twins():
    now = time.time()
    docs = [{"content": "alpha beta gamma delta epsilon", "ts": now - 86400 * 30},
            {"content": "alpha beta gamma delta epsilon", "ts": now},
            {"content": "nothing to see in this row", "ts": now}]
    rows = best_rows("alpha", docs, 5)
    assert [r["ts"] for r in rows] == [now]
    assert best_rows('"quote" alpha', docs, 5)


def test_the_automatic_prefetch_never_reads_raw_records(world):
    assert asyncio.run(world.prefetch("zebra", session_id="now", user_id=USER)) == ""
