"""The ``knowledge`` status section (WS-K1, 2026-09-22).

Three answers, never two: a count, "not recorded yet" (absent file), or
"unreadable (<reason>)" (a file that exists and refused) — the last one with a
health item, because the whole snapshot exists to stop an unreadable store from
rendering as the zero it resembles.
"""
import os
import sqlite3

import pytest

from core.status_knowledge import knowledge_section
from core.status_snapshot import STATE_OK


def _memory_db(home, *, recall=0, curated=0, episodes=(), kb=()):
    db = os.path.join(home, "memory.db")
    con = sqlite3.connect(db)
    con.execute("CREATE VIRTUAL TABLE memories USING fts5("
                "user_id UNINDEXED, session_id UNINDEXED, content)")
    for i in range(recall):
        con.execute("INSERT INTO memories VALUES (?,?,?)", ("rob", "s1", f"fact {i}"))
    con.execute("CREATE TABLE curated_memory (user_id TEXT, content TEXT)")
    for i in range(curated):
        con.execute("INSERT INTO curated_memory VALUES (?,?)", ("rob", f"note {i}"))
    con.execute("CREATE TABLE episodes (user_id TEXT, kind TEXT, ts INTEGER, "
                "surfaced INTEGER)")
    for kind, ts, surfaced in episodes:
        con.execute("INSERT INTO episodes VALUES (?,?,?,?)", ("rob", kind, ts, surfaced))
    con.execute("CREATE TABLE kb_sources (user_id TEXT, collection TEXT, "
                "source_path TEXT)")
    for collection, path in kb:
        con.execute("INSERT INTO kb_sources VALUES (?,?,?)", ("rob", collection, path))
    con.commit()
    con.close()
    return db


def _artifacts_db(home, n, *, binaries=0):
    db = os.path.join(home, "artifacts.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE artifacts (id TEXT, user_id TEXT, path TEXT)")
    for i in range(n):
        con.execute("INSERT INTO artifacts VALUES (?,?,?)", (str(i), "rob", f"/p/{i}.md"))
    for i in range(binaries):
        con.execute("INSERT INTO artifacts VALUES (?,?,?)",
                    (f"b{i}", "rob", f"/p/shot{i}.PNG"))
    con.commit()
    con.close()
    return db


def _skill_usage_db(home, rows):
    db = os.path.join(home, "skill_usage.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE skill_usage (skill_id TEXT, user_id TEXT, "
                "load_count INTEGER, last_used_at REAL)")
    for skill_id, count in rows:
        con.execute("INSERT INTO skill_usage VALUES (?,?,?,?)",
                    (skill_id, "rob", count, 1_700_000_000.0))
    con.commit()
    con.close()
    return db


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "sessions"))
    return str(tmp_path)


def test_counts_every_store_and_leads_with_one_headline(home):
    _memory_db(home, recall=12, curated=2,
               episodes=[("goal", 1_700_000_000, 1), ("goal", 1_700_000_100, 0),
                         ("cron", 1_700_000_200, 0)],
               kb=[("default", "a.md"), ("default", "b.md"), ("x402", "c.md")])
    _artifacts_db(home, 10)
    _skill_usage_db(home, [("web-research", 3), ("never", 0)])

    sec = knowledge_section("rob", home, now=1_700_000_300)

    assert sec.state == STATE_OK and not sec.health
    body = "\n".join(sec.lines)
    assert sec.lines[0].startswith("12 recalled · 3 episodes · 2 curated · 3 indexed · 10 written")
    assert "recall: 12 rows" in body
    assert "curated: 2 notes" in body
    assert "33% surfaced" in body          # 1 of 3
    assert "3 sources in 2 collection(s)" in body
    assert "10 registered (10 text), 3 indexed (30%)" in body
    assert "skills: 1 ever loaded" in body  # load_count > 0 only


def test_an_absent_store_is_not_a_zero_and_is_not_a_fault(home):
    # No memory.db, no artifacts.db, no skill_usage.db, no session root.
    sec = knowledge_section("rob", home)

    assert sec.health == []                      # absent is not a defect
    assert sec.lines == ["nothing recorded yet"]
    assert sec.data["recall"] == {"state": "absent"}
    assert "0" not in sec.lines[0]               # never a confident zero


def test_an_unreadable_store_says_so_and_raises_one_health_item(home):
    with open(os.path.join(home, "memory.db"), "wb") as fh:
        fh.write(b"this is not a database")
    _artifacts_db(home, 4)

    sec = knowledge_section("rob", home)

    keys = [h.key for h in sec.health]
    assert keys and set(keys) == {"knowledge_unreadable"}
    assert any("unreadable" in ln for ln in sec.lines)
    assert sec.data["recall"]["state"] == "error"
    # the readable store still reports
    assert any("documents: 4 registered (4 text)" in ln for ln in sec.lines)
    # and the headline marks the unreadable one, never as zero
    assert sec.lines[0].startswith("? recalled")


def test_the_distilled_store_being_empty_is_stated_not_implied(home):
    _memory_db(home, recall=5, curated=0)
    sec = knowledge_section("rob", home)
    assert any("curated: 0 notes — nothing distilled yet" in ln for ln in sec.lines)


def test_sessions_are_counted_and_aged_never_sized(home, tmp_path):
    root = tmp_path / "sessions" / "rob"
    root.mkdir(parents=True)
    fresh, stale = root / "new", root / "old"
    fresh.mkdir()
    stale.mkdir()
    old = 1_700_000_000 - 200 * 86400
    os.utime(stale, (old, old))

    sec = knowledge_section("rob", home, now=1_700_000_000)

    got = sec.data["sessions"]
    assert got["trees"] == 2 and got["older"] == 1
    assert any("2 trees, 1 older than 90d" in ln for ln in sec.lines)


def test_tenant_scoped(home):
    db = _memory_db(home, recall=3)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO memories VALUES (?,?,?)", ("someone-else", "s", "theirs"))
    con.commit()
    con.close()

    sec = knowledge_section("rob", home)
    assert any("recall: 3 rows" in ln for ln in sec.lines)


def test_unindexed_documents_raise_one_health_item_naming_a_verb(home):
    _memory_db(home, recall=1, kb=[("default", "a.md")])
    _artifacts_db(home, 50)                      # 1 of 50 indexed = 2%

    sec = knowledge_section("rob", home)

    items = [h for h in sec.health if h.key == "knowledge_unindexed"]
    assert len(items) == 1
    assert "49 of 50" in items[0].text
    assert items[0].remedy == "polyrob kb reindex"   # a verb, never an env flag


def test_no_index_health_item_without_a_knowledge_base(home):
    """An instance with no KB is not failing to index — it has nowhere to."""
    _artifacts_db(home, 50)                      # no memory.db at all
    sec = knowledge_section("rob", home)
    assert [h.key for h in sec.health] == []


def test_no_index_health_item_below_the_floor(home):
    _memory_db(home, kb=[])
    _artifacts_db(home, 5)
    sec = knowledge_section("rob", home)
    assert [h.key for h in sec.health] == []


def test_unretained_sessions_raise_one_health_item(home, tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "90")
    root = tmp_path / "sessions" / "rob"
    root.mkdir(parents=True)
    stale = root / "old"
    stale.mkdir()
    old = 1_700_000_000 - 200 * 86400
    os.utime(stale, (old, old))

    sec = knowledge_section("rob", home, now=1_700_000_000)

    items = [h for h in sec.health if h.key == "sessions_unretained"]
    assert len(items) == 1
    assert items[0].remedy == "polyrob sessions prune"
    assert "1 session trees are older than the 90-day window" in items[0].text


def test_retention_off_is_a_choice_not_a_defect(home, tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "0")
    root = tmp_path / "sessions" / "rob"
    root.mkdir(parents=True)
    stale = root / "old"
    stale.mkdir()
    old = 1_700_000_000 - 200 * 86400
    os.utime(stale, (old, old))

    sec = knowledge_section("rob", home, now=1_700_000_000)

    assert [h.key for h in sec.health] == []
    assert any("no retention policy" in ln for ln in sec.lines)


def test_the_indexed_ratio_counts_text_documents_not_screenshots(home):
    """⚠️ Comparing indexed sources against every artifact reports a gap no
    ingest could ever close — the confident-but-wrong shape this section
    exists to avoid."""
    _memory_db(home, kb=[("default", f"{i}.md") for i in range(9)])
    _artifacts_db(home, 10, binaries=90)

    sec = knowledge_section("rob", home)

    body = "\n".join(sec.lines)
    assert "100 registered (10 text), 9 indexed (90%)" in body
    assert [h.key for h in sec.health] == []   # 90% indexed is not a defect
