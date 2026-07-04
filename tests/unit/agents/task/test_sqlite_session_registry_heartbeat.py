"""Task 2.2 — heartbeat/reap wiring + boot-id ownership (P6 SQLite registry).

heartbeat()/reap_stale() were dead code and last_seen_at was frozen at register()
time, so reaping would delete LIVE sessions and stale dead-PID rows caused 409
loops. Ownership keyed on a bare (reusable) PID misroutes on PID reuse.

These tests assert:
  1. heartbeat refreshes last_seen_at so a fresh session survives reap.
  2. reap removes a row that is genuinely older than the ttl.
  3. ownership distinguishes by a per-process boot id, not the PID, so a second
     process (new boot id) with the SAME pid does not consider the session local.
"""
import os

from agents.task.sqlite_session_registry import SqliteSessionRegistry


def _reg(tmp_path):
    return SqliteSessionRegistry(str(tmp_path / "registry.db"))


def _dead_pid():
    """A pid that is not a live process on this host (for dead-worker simulation)."""
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


class _Orch:
    def __init__(self, sid):
        self.session_id = sid


def test_heartbeat_refreshes_and_reap_spares_fresh(tmp_path):
    r = _reg(tmp_path)
    r.register("s1", _Orch("s1"))
    r.heartbeat("s1")
    reaped = r.reap_stale(ttl_seconds=300)
    assert "s1" not in reaped
    assert r.exists("s1") is True


def test_reap_removes_truly_stale(tmp_path):
    db = str(tmp_path / "registry.db")
    # A truly abandoned row: owned by a DEAD worker pid, stale, not held locally.
    dead = SqliteSessionRegistry(db, worker_pid=_dead_pid())
    dead.register("s1", _Orch("s1"))
    dead._orchestrators.pop("s1", None)
    dead._set_last_seen("s1", "2000-01-01T00:00:00")
    reaped = dead.reap_stale(ttl_seconds=300)
    assert "s1" in reaped
    assert dead.exists("s1") is False


def test_reap_spares_locally_held_idle_session(tmp_path):
    """A session still held in this process's _orchestrators must NOT be reaped
    even if its last_seen_at is stale (idle between turns)."""
    r = _reg(tmp_path)
    r.register("idle1", _Orch("idle1"))
    # This process holds the live orchestrator (register already put it in the map).
    assert "idle1" in r._orchestrators
    # Force its heartbeat far into the past (idle between turns):
    r._set_last_seen("idle1", "2000-01-01T00:00:00")
    reaped = r.reap_stale(ttl_seconds=300)
    assert "idle1" not in reaped           # NOT reaped — still locally held
    assert r.exists("idle1") is True
    assert "idle1" in r._orchestrators


def test_reap_removes_stale_not_locally_held_dead_worker(tmp_path):
    """A stale row NOT held locally AND owned by a dead worker pid is reaped."""
    db = str(tmp_path / "registry.db")
    dead = SqliteSessionRegistry(db, worker_pid=_dead_pid())
    dead.register("dead1", _Orch("dead1"))
    dead._orchestrators.pop("dead1", None)
    dead._set_last_seen("dead1", "2000-01-01T00:00:00")
    reaped = dead.reap_stale(ttl_seconds=300)
    assert "dead1" in reaped
    assert dead.exists("dead1") is False


def test_reap_spares_stale_row_of_live_other_worker(tmp_path):
    """B4: a stale row owned by ANOTHER worker that is still ALIVE (idle session,
    frozen heartbeat) must NOT be reaped — deleting it would false-404 and orphan
    that worker's orchestrator. Simulate with the live test-process pid, seen from a
    reaper that does not hold the orchestrator locally."""
    db = str(tmp_path / "registry.db")
    # Worker A (the live owner, this process's pid) registers + idles.
    owner = SqliteSessionRegistry(db, worker_pid=os.getpid())
    owner.register("idle-remote", _Orch("idle-remote"))
    owner._set_last_seen("idle-remote", "2000-01-01T00:00:00")

    # Worker B (a different registry instance / reaper) does NOT hold the orchestrator.
    reaper = SqliteSessionRegistry(db, worker_pid=os.getpid())
    reaper._orchestrators.pop("idle-remote", None)
    reaped = reaper.reap_stale(ttl_seconds=300)

    assert "idle-remote" not in reaped   # owner pid is alive → spared
    assert reaper.exists("idle-remote") is True


def test_ownership_uses_boot_id(tmp_path):
    db = str(tmp_path / "registry.db")
    same_pid = os.getpid()

    # Worker A registers the session.
    r1 = SqliteSessionRegistry(db, worker_pid=same_pid)
    r1.register("s1", _Orch("s1"))
    assert r1.route("s1").is_local is True
    assert r1._boot_id != ""

    # Worker B: a DIFFERENT process (fresh boot id) but the SAME pid value
    # (simulating PID reuse). It must NOT consider the session locally owned.
    r2 = SqliteSessionRegistry(db, worker_pid=same_pid)
    assert r2._boot_id != r1._boot_id
    route = r2.route("s1")
    assert route.is_local is False
    assert route.is_remote is True
    # And the stored boot id of the owner differs from r2's.
    assert r2.owner_boot_id("s1") == r1._boot_id
    assert r2.owner_boot_id("s1") != r2._boot_id
