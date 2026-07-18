"""Wave 1 Task 5 — bulk label-filtered corpus export."""
import json
import sqlite3

import pytest

from datagen.bulk_export import bulk_export, iter_session_dirs


def _mk_session(data_root, user_id, session_id, *, messages=None):
    sdir = data_root / user_id / "sessions" / session_id
    hist = sdir / "memory" / "message_history.json"
    hist.parent.mkdir(parents=True, exist_ok=True)
    hist.write_text(json.dumps({
        "session_id": session_id,
        "saved_at": "2026-07-11T09:00:00",
        "messages": messages or [
            {"type": "HumanMessage", "content": "task", "origin": "USER"},
            {"type": "AIMessage", "content": "did it"},
        ],
    }))
    return sdir


def _mk_memory_db(data_root, rows):
    db = data_root / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts INTEGER NOT NULL, started_ts INTEGER, user_id TEXT NOT NULL, "
        "session_id TEXT NOT NULL, thread_key TEXT, kind TEXT NOT NULL, "
        "task TEXT, outcome TEXT, summary TEXT, "
        "artifacts TEXT NOT NULL DEFAULT '[]', "
        "spend_usd REAL NOT NULL DEFAULT 0, steps INTEGER NOT NULL DEFAULT 0, "
        "goal_id TEXT, surfaced INTEGER NOT NULL DEFAULT 0, "
        "meta TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL)")
    for i, (uid, sid, outcome) in enumerate(rows):
        conn.execute(
            "INSERT INTO episodes (ts, user_id, session_id, kind, outcome,"
            " created_at) VALUES (?, ?, ?, 'goal', ?, 1)", (i, uid, sid, outcome))
    conn.commit()
    conn.close()
    return db


def test_iter_session_dirs(tmp_path):
    _mk_session(tmp_path, "u1", "s1")
    _mk_session(tmp_path, "u2", "s2")
    found = sorted((u, s) for u, s, _ in iter_session_dirs(tmp_path))
    assert found == [("u1", "s1"), ("u2", "s2")]


def test_iter_session_dirs_reaches_legacy_auto_sibling(tmp_path):
    """H6 (2026-07-14 review): legacy sessions live at ``data/auto/<user>/sessions/<sid>``
    — a SIBLING of the data root (``data/task``), so a walk confined to the root
    silently excluded them from every exported corpus."""
    data_root = tmp_path / "task"
    _mk_session(data_root, "u1", "s1")                      # canonical root session
    _mk_session(tmp_path / "auto", "u2", "s_legacy")        # legacy sibling session
    found = sorted((u, s) for u, s, _ in iter_session_dirs(data_root))
    assert found == [("u1", "s1"), ("u2", "s_legacy")]


def test_iter_session_dirs_auto_sibling_no_duplicates_or_junk(tmp_path):
    """The auto/ walk is marker-gated and dedup'd against the root walk."""
    data_root = tmp_path / "task"
    _mk_session(data_root, "u1", "s1")
    # junk dir under auto/ without session markers must not be yielded
    (tmp_path / "auto" / "u9" / "sessions" / "not_a_session").mkdir(parents=True)
    found = sorted((u, s) for u, s, _ in iter_session_dirs(data_root))
    assert found == [("u1", "s1")]


def test_bulk_export_filters_by_outcome(tmp_path):
    _mk_session(tmp_path, "u1", "s1")
    _mk_session(tmp_path, "u1", "s2")
    _mk_memory_db(tmp_path, [("u1", "s1", "done"), ("u1", "s2", "failed")])
    out = tmp_path / "corpus.jsonl"
    stats = bulk_export(tmp_path, out, "sharegpt", {"outcome": "done"},
                        include_correspondent=False, limit=None)
    assert stats["exported"] == 1
    assert stats["skipped_filter"] == 1
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["labels"]["outcome"] == "done"


def test_bulk_export_skips_correspondent(tmp_path):
    _mk_session(tmp_path, "u1", "s1", messages=[
        {"type": "HumanMessage", "content": "reply",
         "origin": "CORRESPONDENT"}])
    out = tmp_path / "c.jsonl"
    stats = bulk_export(tmp_path, out, "raw", {}, include_correspondent=False,
                        limit=None)
    assert stats["exported"] == 0
    assert stats["skipped_correspondent"] == 1
    stats2 = bulk_export(tmp_path, tmp_path / "c2.jsonl", "raw", {},
                         include_correspondent=True, limit=None)
    assert stats2["exported"] == 1


def test_bulk_export_scrub_failure_is_counted_not_raised(tmp_path, monkeypatch):
    _mk_session(tmp_path, "u1", "s1")
    import datagen.bulk_export as be

    def boom(record):
        raise be.ScrubError("nope")

    monkeypatch.setattr(be, "scrub_record", boom)
    stats = bulk_export(tmp_path, tmp_path / "x.jsonl", "raw", {},
                        include_correspondent=False, limit=None)
    assert stats["exported"] == 0
    assert stats["skipped_scrub"] == 1


def test_bulk_export_limit_and_user_filter(tmp_path):
    _mk_session(tmp_path, "u1", "s1")
    _mk_session(tmp_path, "u2", "s2")
    stats = bulk_export(tmp_path, tmp_path / "l.jsonl", "raw", {},
                        include_correspondent=False, limit=1)
    assert stats["exported"] == 1
    stats2 = bulk_export(tmp_path, tmp_path / "u.jsonl", "raw", {},
                         include_correspondent=False, limit=None, user_id="u2")
    assert stats2["exported"] == 1


# --- finalization B-2/B-4 regressions: the REAL on-disk layouts ---------------


def _mk_session_direct(data_root, user_id, session_id, *, messages=None):
    """Canonical live layout: <data_root>/<user_id>/<session_id>/ — sessions
    are created DIRECTLY under the user (agents/task/path.py
    get_session_root: no "sessions" subdirectory)."""
    sdir = data_root / user_id / session_id
    hist = sdir / "memory" / "message_history.json"
    hist.parent.mkdir(parents=True, exist_ok=True)
    hist.write_text(json.dumps({
        "session_id": session_id,
        "saved_at": "2026-07-12T09:00:00",
        "messages": messages or [
            {"type": "HumanMessage", "content": "task", "origin": "USER"},
            {"type": "AIMessage", "content": "did it"},
        ],
    }))
    return sdir


def test_iter_session_dirs_direct_layout(tmp_path):
    """REGRESSION: the shipped glob only matched */sessions/* — the canonical
    direct layout (server data/task/<user>/<sess>, local <home>/sessions/
    <user>/<sess>) yielded NOTHING, so `polyrob datagen export` exported an
    empty corpus on every real deployment."""
    _mk_session_direct(tmp_path, "u1", "s1")
    _mk_session_direct(tmp_path, "u2", "s2")
    # non-session noise that must not be yielded
    (tmp_path / "datagen" / "runs" / "myrun").mkdir(parents=True)
    found = sorted((u, s) for u, s, _ in iter_session_dirs(tmp_path))
    assert found == [("u1", "s1"), ("u2", "s2")]


def test_iter_session_dirs_mixed_layouts_no_duplicates(tmp_path):
    _mk_session(tmp_path, "u1", "s1")          # legacy */sessions/* layout
    _mk_session_direct(tmp_path, "u2", "s2")   # canonical direct layout
    found = sorted((u, s) for u, s, _ in iter_session_dirs(tmp_path))
    assert found == [("u1", "s1"), ("u2", "s2")]
    # the legacy container dir itself must never be yielded as a session
    assert ("u1", "sessions") not in found


def test_bulk_export_direct_layout_with_parent_memory_db(tmp_path):
    """REGRESSION: memory.db lives in BotConfig.data_dir — the PARENT of
    pm().data_root on the local CLI (<home>/sessions) — so label lookups
    against <data_root>/memory.db silently missed and every record exported
    outcome=unknown."""
    home = tmp_path
    data_root = home / "sessions"
    _mk_session_direct(data_root, "u1", "s1")
    _mk_session_direct(data_root, "u1", "s2")
    _mk_memory_db(home, [("u1", "s1", "done"), ("u1", "s2", "failed")])
    out = home / "corpus.jsonl"
    stats = bulk_export(data_root, out, "raw", {"outcome": "done"},
                        include_correspondent=False, limit=None)
    assert stats["exported"] == 1
    line = json.loads(out.read_text().strip())
    assert line["labels"]["outcome"] == "done"
