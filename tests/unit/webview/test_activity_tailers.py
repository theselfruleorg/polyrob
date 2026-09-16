"""Tasks 2+3 — SQLite id-cursor tails + feed path derivation (webview/activity.py)."""
import json
import sqlite3

import pytest

from webview.activity import SqliteTail, feed_path_info, normalize_db_event


@pytest.fixture()
def telemetry_db(tmp_path):
    db = tmp_path / "telemetry_events.db"
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE telemetry_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
               kind TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '',
               session_id TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
               attrs TEXT NOT NULL DEFAULT '{}')"""
    )
    con.execute(
        "INSERT INTO telemetry_events (ts, kind, user_id, attrs) VALUES (1.0, 'cron_run', 'rob', ?)",
        (json.dumps({"job_id": "j1", "outcome": "ok"}),),
    )
    con.commit()
    con.close()
    return str(db)


def _insert_telemetry(db_path, kind, ts):
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO telemetry_events (ts, kind, user_id, attrs) VALUES (?, ?, 'rob', '{}')",
        (ts, kind),
    )
    con.commit()
    con.close()


def test_tail_primes_at_max_and_polls_only_new(telemetry_db):
    tail = SqliteTail(telemetry_db, "telemetry_events")
    tail.prime()
    assert tail.poll() == []  # pre-existing row 1 not flooded
    _insert_telemetry(telemetry_db, "self_wake", 2.0)
    _insert_telemetry(telemetry_db, "wallet_spend", 3.0)
    rows = tail.poll()
    assert [r["kind"] for r in rows] == ["self_wake", "wallet_spend"]
    assert tail.poll() == []  # cursor advanced


def test_tail_missing_db_is_silent(tmp_path):
    tail = SqliteTail(str(tmp_path / "nope.db"), "telemetry_events")
    tail.prime()
    assert tail.poll() == []


def test_tail_db_appearing_after_prime_is_picked_up(tmp_path, telemetry_db):
    import shutil
    target = tmp_path / "late.db"
    tail = SqliteTail(str(target), "telemetry_events")
    tail.prime()  # nothing there yet
    shutil.copy(telemetry_db, target)  # db appears with 1 historic row
    _insert_telemetry(str(target), "self_wake", 2.0)
    kinds = [r["kind"] for r in tail.poll()]
    # Late-appearing DB: everything with id > 0 is new to us; must not raise.
    assert "self_wake" in kinds


def test_goal_events_tail_normalizes(tmp_path):
    db = tmp_path / "goals.db"
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE goal_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
               goal_id TEXT, kind TEXT, payload TEXT, created_at REAL)"""
    )
    con.execute(
        "INSERT INTO goal_events (goal_id, kind, payload, created_at) VALUES ('g1','claimed','{}',5.0)"
    )
    con.commit()
    con.close()
    tail = SqliteTail(str(db), "goal_events")
    # prime with cursor 0 → poll returns the row (used by backfill seeding)
    rows = tail.poll()
    assert len(rows) == 1
    ev = normalize_db_event("goal", rows[0])
    assert ev["kind"] == "goal_claimed" and ev["id"] == "goal:1"


# --- Task 3: feed path derivation ---------------------------------------- #

def test_feed_path_info_accepts_canonical_layout(tmp_path):
    root = str(tmp_path)
    p = f"{root}/rob/sess-123/feed/000007_step_0002.json"
    assert feed_path_info(p, root) == ("rob", "sess-123")


def test_feed_path_info_rejects_non_feed_paths(tmp_path):
    root = str(tmp_path)
    assert feed_path_info(f"{root}/telemetry_events.db-wal", root) is None
    assert feed_path_info(f"{root}/rob/sess-1/screenshots/step_1.jpg", root) is None
    assert feed_path_info(f"{root}/rob/sess-1/feed/x.tmp", root) is None
    assert feed_path_info(f"{root}/rob/feed/x.json", root) is None  # missing session level
    assert feed_path_info("/elsewhere/rob/s/feed/x.json", root) is None


# --- the goal tail carries the goal's TENANT ------------------------------ #
# ``goal_events`` rows have no ``user_id`` column and their payloads do not carry
# one, so ``normalize_db_event("goal", …)`` stamped every goal event ``user_id=""``
# and the tenant-scoped Work › Log (``worklog_api.py``) dropped the whole goal
# board. The tenant is one join away, on ``goals.user_id``.

def _goal_db_with_owner(tmp_path):
    db = tmp_path / "goals.db"
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE goal_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
               goal_id TEXT, kind TEXT, payload TEXT, created_at REAL)"""
    )
    con.execute(
        """CREATE TABLE goals (id TEXT PRIMARY KEY, user_id TEXT, title TEXT,
               status TEXT)"""
    )
    con.execute("INSERT INTO goals VALUES ('g1', 'rob', 'Engage the den', 'running')")
    con.execute(
        "INSERT INTO goal_events (goal_id, kind, payload, created_at) "
        "VALUES ('g1','claimed','{}',5.0)"
    )
    con.execute(
        "INSERT INTO goal_events (goal_id, kind, payload, created_at) "
        "VALUES ('orphan','created','{\"user_id\": \"u2\"}',6.0)"
    )
    con.commit()
    con.close()
    return db


def test_goal_tail_stamps_the_goal_owner_as_the_event_tenant(tmp_path):
    from webview.activity import goal_events_tail
    tail = goal_events_tail(str(_goal_db_with_owner(tmp_path)))
    rows = tail.poll()
    assert [r["id"] for r in rows] == [1, 2]
    ev = normalize_db_event("goal", rows[0])
    assert ev["user_id"] == "rob"
    assert ev["payload"].get("title") == "Engage the den"
    # A payload that names its own tenant still wins over the join.
    assert normalize_db_event("goal", rows[1])["user_id"] == "u2"
    # The cursor advanced on the joined rows too.
    assert tail.cursor == 2 and tail.poll() == []


def test_goal_tail_cold_backfill_uses_the_same_join(tmp_path):
    from webview.activity import goal_events_tail
    tail = goal_events_tail(str(_goal_db_with_owner(tmp_path)))
    rows = tail.recent(10)
    assert [r["id"] for r in rows] == [2, 1]
    assert rows[1]["goal_user_id"] == "rob"


def test_goal_tail_without_a_goals_table_still_delivers_untenanted(tmp_path):
    """A DB with no ``goals`` table (legacy / test fixture) must not silence the
    stream: the join is dropped and the row arrives with an empty tenant."""
    from webview.activity import goal_events_tail
    db = tmp_path / "goals.db"
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE goal_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
               goal_id TEXT, kind TEXT, payload TEXT, created_at REAL)"""
    )
    con.execute("INSERT INTO goal_events (goal_id, kind, payload, created_at) "
                "VALUES ('g1','created','{}',1.0)")
    con.commit()
    con.close()
    tail = goal_events_tail(str(db))
    rows = tail.poll()
    assert len(rows) == 1 and tail.cursor == 1
    assert normalize_db_event("goal", rows[0])["user_id"] == ""
    assert len(tail.recent(5)) == 1
