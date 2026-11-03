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


# --- 098 AG-16: owner-guarded rows, liveness by process identity -------------

def test_register_does_not_steal_live_owner(tmp_path):
    a = _reg(tmp_path)
    b = _reg(tmp_path)
    assert a.register("s1", _Orch("s1")) is True
    assert b.register("s1", _Orch("s1")) is False          # a is alive: no theft
    assert b.owner_boot_id("s1") == a._boot_id
    assert b.route("s1").is_remote
    assert a.route("s1").is_local


def test_remove_only_deletes_own_row(tmp_path):
    a = _reg(tmp_path)
    b = _reg(tmp_path)
    a.register("s1", _Orch("s1"))
    b.register("s1", _Orch("s1"))                          # refused, held locally
    assert b.remove("s1") is not None                      # local object dropped
    assert a.exists("s1") is True                          # a's row stays
    assert a.route("s1").is_local


def test_register_takes_over_dead_owner(tmp_path):
    db = str(tmp_path / "registry.db")
    dead = SqliteSessionRegistry(db, worker_pid=_dead_pid())
    dead.register("s1", _Orch("s1"))
    live = SqliteSessionRegistry(db)
    assert live.register("s1", _Orch("s1")) is True
    assert live.owner_boot_id("s1") == live._boot_id
    assert live.route("s1").is_local


def test_route_dead_owner_returns_missing(tmp_path):
    db = str(tmp_path / "registry.db")
    SqliteSessionRegistry(db, worker_pid=_dead_pid()).register("s1", _Orch("s1"))
    live = SqliteSessionRegistry(db)
    assert live.route("s1").is_missing
    assert live.exists("s1") is False


def test_reused_pid_reads_dead(tmp_path):
    """A row stored by a previous life of OUR pid (other start time) is dead."""
    import sqlite3
    from core.state.proc import ProcIdentity, boot_id
    db = str(tmp_path / "registry.db")
    live = SqliteSessionRegistry(db)
    old = ProcIdentity(os.getpid(), "a-start-time-long-ago", boot_id()).encode()
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO active_sessions (session_id, worker_pid, owner_boot_id, owner_ident, "
        "params, last_seen_at, created_at) VALUES ('s1', ?, 'old-boot', ?, '{}', 'x', 'x')",
        (os.getpid(), old))
    con.commit(); con.close()
    assert live.route("s1").is_missing


def test_legacy_row_of_our_pid_reads_dead(tmp_path):
    """A pre-identity row with our pid and another boot id is a previous life."""
    import sqlite3
    db = str(tmp_path / "registry.db")
    live = SqliteSessionRegistry(db)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO active_sessions (session_id, worker_pid, owner_boot_id, params, "
        "last_seen_at, created_at) VALUES ('s1', ?, 'old-boot', '{}', 'x', 'x')",
        (os.getpid(),))
    con.commit(); con.close()
    assert live.route("s1").is_missing


def test_status_column_not_written(tmp_path):
    """S10: the dead ``status`` column is gone from a fresh DB; an old DB keeps it
    (default 'active') and registration still works."""
    import sqlite3
    db = str(tmp_path / "fresh.db")
    SqliteSessionRegistry(db).register("s1", _Orch("s1"))
    con = sqlite3.connect(db)
    cols = {row[1] for row in con.execute("PRAGMA table_info(active_sessions)")}
    con.close()
    assert "status" not in cols and "owner_ident" in cols


def test_recreate_refuses_a_session_a_live_peer_owns(tmp_path):
    """098 review: a second worker must not rebuild (and run) a session that a
    live worker owns — the registry refused the row, but the object was still
    held and run locally, so two orchestrators wrote one session's files."""
    import asyncio
    from agents.task.task_agent_delivery import TaskAgentDeliveryMixin

    a, b = _reg(tmp_path), _reg(tmp_path)
    a.register("s1", _Orch("s1"))
    built = []

    class _Agent(TaskAgentDeliveryMixin):
        _registry = b

        def route_session(self, sid):
            return self._registry.route(sid)

        def _heal_narrowed_request(self, sid):
            pass

        async def _recreate_orchestrator(self, sid, info):
            built.append(sid)
            orch = _Orch(sid)
            self._registry.register(sid, orch)
            return orch

    agent = _Agent()
    assert asyncio.run(agent._resolve_or_recreate("s1", {})) is None
    assert built == []
    a.remove("s1")                                    # the peer lets go
    assert asyncio.run(agent._resolve_or_recreate("s1", {})) is not None
    assert built == ["s1"]
