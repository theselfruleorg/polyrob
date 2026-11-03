"""098 W0: the core/state primitives."""
import asyncio
import json
import os
import sqlite3
import threading

import pytest

from core.state.files import Replace, atomic_write_json, locked_update_json, read_json
from core.state.machine import REGISTRY, IllegalTransition, StateMachine
from core.state.once import OnceLatch
from core.state.proc import ProcIdentity, is_alive, process_identity
from core.state.sql import acas_transition, cas_transition, json_patch

M = StateMachine("test.job", states={"queued", "running", "done", "failed", "cancelled"},
                 transitions={"queued": {"running", "cancelled"},
                              "running": {"done", "failed", "cancelled"},
                              "failed": {"queued"}},
                 terminal={"done", "cancelled"}, aliases={"error": "failed"})


# -- machine ----------------------------------------------------------------

def test_machine_edges_and_registry():
    assert REGISTRY["test.job"] is M
    assert M.allowed("queued", "running") and not M.allowed("done", "queued")
    assert M.allowed("error", "queued")  # alias
    assert M.sources_of("cancelled") == {"queued", "running"}
    assert M.validate() == []


@pytest.fixture
def lenient():
    from core.state import machine
    machine.set_strict(False)
    yield
    machine.set_strict(True)


def test_illegal_edge_raises_in_strict_and_refuses_otherwise(lenient):
    from core.state import machine
    machine.set_strict(True)
    with pytest.raises(IllegalTransition):
        M.check("done", "running")
    machine.set_strict(False)
    assert M.check("done", "running") is False


def test_bad_table_rejected():
    with pytest.raises(ValueError):
        StateMachine("test.bad", states={"a"}, transitions={"a": {"b"}})
    with pytest.raises(ValueError):
        StateMachine("test.bad2", states={"a", "b"}, transitions={"b": {"a"}}, terminal={"b"})


def test_validate_finds_dead_end_and_unreachable():
    m = StateMachine("test.dead", states={"a", "b", "c", "z"},
                     transitions={"a": {"b"}, "z": {"a"}}, initial={"a"})
    probs = m.validate()
    assert any("'b' has no exit" in p for p in probs)
    assert any("'z' is unreachable" in p for p in probs)


# -- sql --------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "s.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, owner TEXT, payload TEXT)")
    c.execute("INSERT INTO jobs VALUES ('j1','queued',NULL,'{\"a\":1,\"keep\":true}')")
    c.commit()
    c.close()
    return p


def _row(db):
    c = sqlite3.connect(db)
    r = c.execute("SELECT status, owner, payload FROM jobs WHERE id='j1'").fetchone()
    c.close()
    return r[0], r[1], json.loads(r[2])


def test_cas_moves_once_and_misses_after(db):
    kw = dict(key="id=?", key_params=("j1",), machine=M)
    assert cas_transition(db, "jobs", to="running", from_states=["queued"],
                          set_cols={"owner": "p1"}, **kw)
    assert not cas_transition(db, "jobs", to="running", from_states=["queued"], **kw)
    assert _row(db)[:2] == ("running", "p1")


def test_cas_default_sources_and_owner_predicate(db):
    kw = dict(key="id=?", key_params=("j1",), machine=M)
    cas_transition(db, "jobs", to="running", set_cols={"owner": "p1"}, **kw)
    assert not cas_transition(db, "jobs", to="done", where_extra="owner=?", where_params=("p2",),
                              **kw)
    assert cas_transition(db, "jobs", to="done", where_extra="owner=?", where_params=("p1",),
                          json_set={"result": {"ok": True}, "n": 3}, **kw)
    status, _, payload = _row(db)
    assert status == "done" and payload == {"a": 1, "keep": True, "result": {"ok": True}, "n": 3}


def test_cas_forbidden_from_state_refused(db, lenient):
    assert not cas_transition(db, "jobs", key="id=?", key_params=("j1",), machine=M,
                              to="queued", from_states=["done"])
    assert _row(db)[0] == "queued"


def test_json_patch_keeps_other_keys(db):
    assert json_patch(db, "jobs", key="id=?", key_params=("j1",), set={"b": [1, 2]})
    assert _row(db)[2] == {"a": 1, "keep": True, "b": [1, 2]}


def test_acas_on_caller_connection(db):
    class FakeDB:
        def __init__(self):
            self.c = sqlite3.connect(db)

        async def execute(self, sql, params):
            cur = self.c.execute(sql, params)
            self.c.commit()
            return cur

    fdb = FakeDB()
    ok = asyncio.run(acas_transition(fdb, "jobs", key="id=?", key_params=("j1",), machine=M,
                                     to="cancelled"))
    assert ok and _row(db)[0] == "cancelled"
    again = asyncio.run(acas_transition(fdb, "jobs", key="id=?", key_params=("j1",), machine=M,
                                        to="cancelled", from_states=["queued", "running"]))
    assert not again


def test_bad_identifier_rejected(db):
    with pytest.raises(ValueError):
        cas_transition(db, "jobs; DROP TABLE jobs", key="id=?", key_params=("j1",), machine=M,
                       to="running")


# -- files ------------------------------------------------------------------

def test_atomic_write_and_torn_read(tmp_path):
    p = str(tmp_path / "x" / "s.json")
    atomic_write_json(p, {"a": 1})
    assert read_json(p, {}) == {"a": 1}
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    with open(p, "w") as fh:
        fh.write('{"a": ')
    assert read_json(p, {"d": 1}) == {"d": 1}
    assert read_json(str(tmp_path / "missing.json"), []) == []


def test_locked_update_is_exclusive_across_threads(tmp_path):
    p = str(tmp_path / "c.json")

    def bump():
        for _ in range(25):
            locked_update_json(p, lambda d: d.__setitem__("n", d.get("n", 0) + 1), default=dict)

    ts = [threading.Thread(target=bump) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert read_json(p, {})["n"] == 100


def test_locked_update_replace(tmp_path):
    p = str(tmp_path / "r.json")
    assert locked_update_json(p, lambda d: Replace([1, 2]), default=list) == [1, 2]
    assert read_json(p) == [1, 2]


# -- once -------------------------------------------------------------------

def test_once_sets_only_after_success():
    latch, calls = OnceLatch(), []

    def boom():
        calls.append(1)
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        latch.run(boom)
    assert not latch.done()
    assert latch.run(lambda: calls.append(2)) is True
    assert latch.run(lambda: calls.append(3)) is False
    assert calls == [1, 2]
    latch.reset()
    assert latch.run(lambda: calls.append(4)) is True


def test_once_async():
    latch, calls = OnceLatch(), []

    async def work():
        calls.append(1)

    async def go():
        return await asyncio.gather(latch.arun(work), latch.arun(work))

    assert sorted(asyncio.run(go())) == [False, True] and calls == [1]


# -- proc -------------------------------------------------------------------

def test_process_identity_round_trip_and_alive():
    me = process_identity()
    assert me.pid == os.getpid() and is_alive(me)
    assert ProcIdentity.decode(me.encode()) == me
    assert not is_alive(ProcIdentity(me.pid, "not-the-start", me.boot))
    assert not is_alive(ProcIdentity(me.pid, me.start, "other-boot"))
    assert not is_alive(None) and ProcIdentity.decode("garbage") is None


def test_start_time_does_not_depend_on_the_reader_locale_or_timezone(monkeypatch):
    """098 review: macOS `ps -o lstart=` prints local text; two processes with a
    different TZ/LC_* must still read ONE start time for one pid."""
    import os
    from core.state import proc
    pid = os.getpid()
    base = proc.start_time(pid)
    assert base
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert proc.start_time(pid) == base


def test_proc_read_error_never_falls_back_to_ps(monkeypatch, tmp_path):
    """A /proc read error other than "no such pid" returns None, never a `ps`
    string that can never equal the recorded tick count."""
    import builtins
    from core.state import proc
    if not os.path.isdir("/proc/self"):
        monkeypatch.setattr(proc.os.path, "isdir", lambda p: True)
    real_open = builtins.open

    def _open(path, *a, **k):
        if str(path).startswith("/proc/") and str(path).endswith("/stat"):
            raise PermissionError(path)
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", _open)
    called = []
    monkeypatch.setattr(proc.subprocess, "run", lambda *a, **k: called.append(a))
    assert proc.start_time(1) is None
    assert called == []
