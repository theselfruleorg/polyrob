"""P6 — SQLite-backed session registry: cross-process visibility.

The live orchestrator OBJECT cannot cross processes, so the registry keeps a local
object dict (per worker) AND mirrors session metadata to SQLite so other workers
can SEE the session exists / who owns it (the routing unlock for workers>1).
"""
import os

import pytest

from agents.task.sqlite_session_registry import SqliteSessionRegistry


def _reg(tmp_path, name="w"):
    return SqliteSessionRegistry(str(tmp_path / "registry.db"))


class _Orch:
    def __init__(self, sid):
        self.session_id = sid


def test_register_stores_object_locally_and_metadata_in_sqlite(tmp_path):
    r = _reg(tmp_path)
    o = _Orch("s1")
    r.register("s1", o)
    assert r.get("s1") is o           # local object
    assert r.exists("s1") is True     # SQLite metadata
    assert r.owner_pid("s1") == os.getpid()


def test_get_unknown_returns_none(tmp_path):
    r = _reg(tmp_path)
    assert r.get("nope") is None
    assert r.exists("nope") is False


def test_cross_instance_visibility(tmp_path):
    # two registry instances on the SAME db == two workers sharing state
    r1 = _reg(tmp_path)
    r2 = _reg(tmp_path)
    r1.register("s1", _Orch("s1"))
    # r2 (a different "worker") sees the session exists, but the object isn't local
    assert r2.exists("s1") is True
    assert r2.get("s1") is None
    assert "s1" in r2.global_session_ids()


def test_remove_clears_local_and_sqlite(tmp_path):
    r1 = _reg(tmp_path)
    r2 = _reg(tmp_path)
    r1.register("s1", _Orch("s1"))
    assert r2.exists("s1") is True
    r1.remove("s1")
    assert r1.get("s1") is None
    assert r2.exists("s1") is False


def test_local_count_vs_global_count(tmp_path):
    r1 = _reg(tmp_path)
    r2 = _reg(tmp_path)
    r1.register("a", _Orch("a"))
    r2.register("b", _Orch("b"))
    assert r1.count() == 1            # local objects only
    assert r1.global_count() == 2     # all workers via SQLite


def _dead_pid():
    p = 4_000_000
    while p > 1:
        try:
            os.kill(p, 0)
        except ProcessLookupError:
            return p
        except Exception:
            pass
        p -= 7
    return 4_000_000


def test_heartbeat_and_reap_stale(tmp_path):
    # A truly abandoned row: owned by a DEAD worker pid, stale, not held locally.
    # (A stale row owned by a LIVE worker is an idle session and must be spared —
    # see test_sqlite_session_registry_heartbeat.py.)
    r = SqliteSessionRegistry(str(tmp_path / "registry.db"), worker_pid=_dead_pid())
    r.register("s1", _Orch("s1"))
    r._orchestrators.pop("s1", None)
    r._set_last_seen_for_test("s1", "2000-01-01T00:00:00")
    reaped = r.reap_stale(ttl_seconds=60)
    assert "s1" in reaped
    assert r.exists("s1") is False


def test_interface_compat_with_in_process_registry(tmp_path):
    r = _reg(tmp_path)
    r.register("s1", _Orch("s1"))
    assert "s1" in r                  # __contains__ (local)
    assert r.session_ids() == ["s1"]
    assert len(r) == 1
    r.clear()
    assert len(r) == 0


def test_fresh_db_has_owner_boot_id_column(tmp_path):
    """B11: owner_boot_id is in the CREATE, so a fresh DB never needs the non-idempotent
    ALTER — eliminating the concurrent-boot 'duplicate column' crash on fresh DBs."""
    import sqlite3
    db = str(tmp_path / "fresh.db")
    SqliteSessionRegistry(db)
    con = sqlite3.connect(db)
    cols = {row[1] for row in con.execute("PRAGMA table_info(active_sessions)")}
    con.close()
    assert "owner_boot_id" in cols


def test_migration_of_old_db_is_idempotent(tmp_path):
    """A pre-existing 6-column DB is migrated; a second init on the migrated DB is a
    no-op (column present -> ALTER skipped)."""
    import sqlite3
    db = str(tmp_path / "old.db")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE active_sessions (session_id TEXT PRIMARY KEY, worker_pid INTEGER "
        "NOT NULL, status TEXT NOT NULL DEFAULT 'active', params TEXT NOT NULL DEFAULT "
        "'{}', last_seen_at TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    con.commit(); con.close()
    SqliteSessionRegistry(db)   # migrates
    SqliteSessionRegistry(db)   # second init must not crash
    con = sqlite3.connect(db)
    cols = {row[1] for row in con.execute("PRAGMA table_info(active_sessions)")}
    con.close()
    assert "owner_boot_id" in cols
