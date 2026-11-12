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
        con.execute("INSERT INTO memories VALUES (?,?,?)", ("alice", "s1", f"fact {i}"))
    con.execute("CREATE TABLE curated_memory (user_id TEXT, content TEXT)")
    for i in range(curated):
        con.execute("INSERT INTO curated_memory VALUES (?,?)", ("alice", f"note {i}"))
    con.execute("CREATE TABLE episodes (user_id TEXT, kind TEXT, ts INTEGER, "
                "surfaced INTEGER)")
    for kind, ts, surfaced in episodes:
        con.execute("INSERT INTO episodes VALUES (?,?,?,?)", ("alice", kind, ts, surfaced))
    con.execute("CREATE TABLE kb_sources (user_id TEXT, collection TEXT, "
                "source_path TEXT)")
    for collection, path in kb:
        con.execute("INSERT INTO kb_sources VALUES (?,?,?)", ("alice", collection, path))
    con.commit()
    con.close()
    return db


def _doc(home, name):
    """A real produced file: the section counts only documents that still exist."""
    d = os.path.join(home, "p")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "w") as fh:
        fh.write("x")
    return path


def _artifacts_db(home, n, *, binaries=0, extra=()):
    db = os.path.join(home, "artifacts.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE artifacts (id TEXT, user_id TEXT, path TEXT)")
    for i in range(n):
        con.execute("INSERT INTO artifacts VALUES (?,?,?)",
                    (str(i), "alice", _doc(home, f"{i}.md")))
    for i, path in enumerate(extra):
        con.execute("INSERT INTO artifacts VALUES (?,?,?)", (f"e{i}", "alice", path))
    for i in range(binaries):
        con.execute("INSERT INTO artifacts VALUES (?,?,?)",
                    (f"b{i}", "alice", f"/p/shot{i}.PNG"))
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
                    (skill_id, "alice", count, 1_700_000_000.0))
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
               kb=[("written", _doc(home, "0.md")), ("written", _doc(home, "1.md")),
                   ("x402", "/uploads/c.md")])
    _artifacts_db(home, 10)
    _skill_usage_db(home, [("web-research", 3), ("never", 0)])

    sec = knowledge_section("alice", home, now=1_700_000_300)

    assert sec.state == STATE_OK and not sec.health
    body = "\n".join(sec.lines)
    assert sec.lines[0].startswith("12 memories · 3 episodes · 2 curated · 3 KB sources · 10 documents registered")
    assert "memories: 12 rows" in body
    assert "curated: 2 notes" in body
    assert "33% surfaced" in body          # 1 of 3
    assert "3 sources in 2 collection(s)" in body
    # The upload is a KB source but never an artifact: it is not counted as one
    # of the written documents that are indexed (097 W3 — a join, not a ratio).
    assert "10 registered (10 text, 10 knowledge), 2 in KB" in body
    assert "skills: 1 ever loaded" in body  # load_count > 0 only


def test_an_absent_store_is_not_a_zero_and_is_not_a_fault(home):
    # No memory.db, no artifacts.db, no skill_usage.db, no session root.
    sec = knowledge_section("alice", home)

    assert sec.health == []                      # absent is not a defect
    assert sec.lines == ["nothing recorded yet"]
    assert sec.data["recall"] == {"state": "absent"}
    assert "0" not in sec.lines[0]               # never a confident zero


def test_an_unreadable_store_says_so_and_raises_one_health_item(home):
    with open(os.path.join(home, "memory.db"), "wb") as fh:
        fh.write(b"this is not a database")
    _artifacts_db(home, 4)

    sec = knowledge_section("alice", home)

    keys = [h.key for h in sec.health]
    assert keys and set(keys) == {"knowledge_unreadable"}
    assert any("unreadable" in ln for ln in sec.lines)
    assert sec.data["recall"]["state"] == "error"
    # the readable store still reports
    assert any("documents: 4 registered (4 text, 4 knowledge)" in ln for ln in sec.lines)
    # and the headline marks the unreadable one, never as zero
    assert sec.lines[0].startswith("? memories")


def test_the_distilled_store_being_empty_is_stated_not_implied(home):
    _memory_db(home, recall=5, curated=0)
    sec = knowledge_section("alice", home)
    assert any("curated: 0 notes — nothing distilled yet" in ln for ln in sec.lines)


def test_sessions_are_counted_and_aged_never_sized(home, tmp_path):
    root = tmp_path / "sessions" / "alice"
    root.mkdir(parents=True)
    fresh, stale = root / "new", root / "old"
    fresh.mkdir()
    stale.mkdir()
    old = 1_700_000_000 - 200 * 86400
    os.utime(stale, (old, old))

    sec = knowledge_section("alice", home, now=1_700_000_000)

    got = sec.data["sessions"]
    assert got["trees"] == 2 and got["older"] == 1
    assert any("2 trees, 1 older than 90d" in ln for ln in sec.lines)


def test_tenant_scoped(home):
    db = _memory_db(home, recall=3)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO memories VALUES (?,?,?)", ("someone-else", "s", "theirs"))
    con.commit()
    con.close()

    sec = knowledge_section("alice", home)
    assert any("memories: 3 rows" in ln for ln in sec.lines)


def test_unindexed_documents_are_a_line_never_a_health_item(home):
    """097: the old ratio warning could not be cleared by its own remedy.
    Indexing is automatic; how much is indexed is a fact, not a fault."""
    _memory_db(home, recall=1, kb=[("default", "a.md")])
    _artifacts_db(home, 50)

    sec = knowledge_section("alice", home)

    assert [h.key for h in sec.health] == []
    assert any("50 registered (50 text, 50 knowledge), 0 in KB" in ln
               for ln in sec.lines)


def test_records_copies_and_deleted_files_are_not_knowledge(home):
    """The denominator is what the ingest would accept, not every text file."""
    _memory_db(home, kb=[])
    _artifacts_db(home, 2, extra=[_doc(home, "exit-run-log.md"),
                                  _doc(home, "ledger.md.bak"),
                                  os.path.join(home, "p", "gone.md")])
    sec = knowledge_section("alice", home)
    assert sec.data["documents"]["knowledge"] == 2
    assert [h.key for h in sec.health] == []


def test_a_document_in_two_collections_counts_once(home):
    path_a = _doc(home, "0.md")
    _memory_db(home, kb=[("default", path_a), ("written", path_a)])
    _artifacts_db(home, 1)
    sec = knowledge_section("alice", home)
    assert sec.data["documents"]["indexed"] == 1


def _stale_tree(tmp_path):
    root = tmp_path / "sessions" / "alice"
    root.mkdir(parents=True)
    stale = root / "old"
    stale.mkdir()
    old = 1_700_000_000 - 200 * 86400
    os.utime(stale, (old, old))


def test_old_trees_with_a_recent_sweep_are_not_a_fault(home, tmp_path, monkeypatch):
    """097 W9: the sweep KEEPS trees that hold an artifact or a live goal, so an
    old tree is not evidence that retention failed."""
    from core.session_retention import record_sweep_state
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "90")
    _stale_tree(tmp_path)
    record_sweep_state(home, removed=0, now=1_700_000_000 - 3600)

    sec = knowledge_section("alice", home, now=1_700_000_000)

    assert [h.key for h in sec.health] == []
    assert any("1 older than 90d" in ln for ln in sec.lines)


def test_a_sweep_that_never_ran_is_one_health_item(home, tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "90")
    _stale_tree(tmp_path)

    sec = knowledge_section("alice", home, now=1_700_000_000)

    items = [h for h in sec.health if h.key == "sessions_sweep_not_running"]
    assert len(items) == 1 and items[0].remedy == "polyrob sessions prune"
    assert "has not run since never" in items[0].text


def test_a_stale_sweep_is_one_health_item(home, tmp_path, monkeypatch):
    from core.session_retention import record_sweep_state
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "90")
    _stale_tree(tmp_path)
    record_sweep_state(home, now=1_700_000_000 - 5 * 86400)

    sec = knowledge_section("alice", home, now=1_700_000_000)
    assert [h.key for h in sec.health] == ["sessions_sweep_not_running"]


def test_a_refused_sweep_names_its_reason(home, tmp_path, monkeypatch):
    from core.session_retention import record_sweep_state
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "90")
    _stale_tree(tmp_path)
    record_sweep_state(home, error="goals.db unreadable (DatabaseError: x)",
                       now=1_700_000_000 - 60)

    sec = knowledge_section("alice", home, now=1_700_000_000)
    items = [h for h in sec.health if h.key == "sessions_sweep_refused"]
    assert len(items) == 1 and "goals.db unreadable" in items[0].text


def test_an_unreadable_sweep_record_is_not_never(home, tmp_path, monkeypatch):
    import os
    from core.session_retention import SWEEP_STATE_FILE
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "90")
    _stale_tree(tmp_path)
    with open(os.path.join(home, SWEEP_STATE_FILE), "w", encoding="utf-8") as fh:
        fh.write("{truncat")
    sec = knowledge_section("alice", home, now=1_700_000_000)
    items = [h for h in sec.health if h.key == "sessions_sweep_record_unreadable"]
    assert len(items) == 1 and "unreadable" in items[0].text
    assert not any("since never" in h.text for h in sec.health)


def test_the_remedy_clears_the_sweep_item(home, tmp_path, monkeypatch):
    """H2: the named remedy clears its item — `polyrob sessions prune` records
    a sweep, after which the item is gone."""
    from core.session_retention import record_sweep_state
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "90")
    _stale_tree(tmp_path)
    assert knowledge_section("alice", home, now=1_700_000_000).health
    record_sweep_state(home, now=1_700_000_000)          # what the prune verb writes
    assert knowledge_section("alice", home, now=1_700_000_000).health == []


def test_retention_off_is_a_choice_not_a_defect(home, tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "0")
    root = tmp_path / "sessions" / "alice"
    root.mkdir(parents=True)
    stale = root / "old"
    stale.mkdir()
    old = 1_700_000_000 - 200 * 86400
    os.utime(stale, (old, old))

    sec = knowledge_section("alice", home, now=1_700_000_000)

    assert [h.key for h in sec.health] == []
    assert any("no retention policy" in ln for ln in sec.lines)


def test_the_indexed_count_counts_text_documents_not_screenshots(home):
    """⚠️ Comparing indexed sources against every artifact reports a gap no
    ingest could ever close — the confident-but-wrong shape this section
    exists to avoid."""
    _artifacts_db(home, 10, binaries=90)
    _memory_db(home, kb=[("written", os.path.join(home, "p", f"{i}.md")) for i in range(9)])

    sec = knowledge_section("alice", home)

    body = "\n".join(sec.lines)
    assert "100 registered (10 text, 10 knowledge), 9 in KB" in body
    assert [h.key for h in sec.health] == []
