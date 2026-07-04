"""P6 — cross-worker session routing decision on both registries."""
import os

from agents.task.session_registry import SessionRegistry
from agents.task.sqlite_session_registry import SqliteSessionRegistry
from agents.task.session_route import SessionRoute, LOCAL, REMOTE, MISSING


class _Orch:
    def __init__(self, sid):
        self.session_id = sid


# --- in-process registry: only LOCAL or MISSING ---

def test_inprocess_route_local_and_missing():
    r = SessionRegistry()
    o = _Orch("s1")
    r.register("s1", o)
    route = r.route("s1")
    assert isinstance(route, SessionRoute) and route.is_local and route.orchestrator is o
    assert r.route("nope").is_missing


# --- sqlite registry: LOCAL / REMOTE / MISSING across workers ---

def test_sqlite_route_local(tmp_path):
    r = SqliteSessionRegistry(str(tmp_path / "r.db"))
    r.register("s1", _Orch("s1"))
    route = r.route("s1")
    assert route.is_local and route.orchestrator is not None
    assert route.owner_pid == os.getpid()


def test_sqlite_route_remote(tmp_path):
    # two registry instances on one db == two workers
    w1 = SqliteSessionRegistry(str(tmp_path / "r.db"))
    w2 = SqliteSessionRegistry(str(tmp_path / "r.db"))
    w1.register("s1", _Orch("s1"))
    route = w2.route("s1")
    assert route.is_remote
    assert route.orchestrator is None       # object not in this worker
    assert route.owner_pid == os.getpid()   # same proc in test, but reported from SQLite


def test_sqlite_route_missing(tmp_path):
    r = SqliteSessionRegistry(str(tmp_path / "r.db"))
    assert r.route("ghost").is_missing
