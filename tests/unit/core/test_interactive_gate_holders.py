"""098 CO-01: the human-turn gate is a set of live holders, not a depth."""
import asyncio
import json
import os

import pytest


@pytest.fixture
def gate(monkeypatch, tmp_path):
    from core import interactive_gate as g
    monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLI_WORKSPACE_LOCK", raising=False)
    g.reset_for_tests()
    yield g
    g.reset_for_tests()


def _marker(g):
    return g.read_turn_marker()


def test_overlapping_owner_turns_keep_marker_until_last_ends(gate):
    a = gate.owner_turn(kind="owner_chat", session_id="dm")
    b = gate.owner_turn(kind="room", session_id="room1")
    a.__enter__()
    b.__enter__()
    a.__exit__(None, None, None)              # A ends first (not LIFO)
    m = _marker(gate)
    assert m and [h["session_id"] for h in m["holders"]] == ["room1"]
    assert gate._gate_state == "held" and gate.is_interactive_busy()
    b.__exit__(None, None, None)
    assert _marker(gate) is None and gate._gate_state == "idle"
    assert not gate.is_interactive_busy()


def test_repl_turn_during_goal_run_writes_marker(gate):
    goal = gate.mark_busy("goal", "g1")       # a non-human holder
    with gate.interactive_turn():
        m = _marker(gate)
        assert m and m["kind"] == "repl"
        assert gate._gate_lock is not None or gate._workspace_lock_path() is None
    assert _marker(gate) is None
    assert gate.is_interactive_busy()         # the goal run still holds the gate
    gate.mark_idle(goal)
    assert not gate.is_interactive_busy()


def test_mark_idle_unknown_token_does_not_release_owner_turn(gate):
    with gate.owner_turn(kind="owner_chat", session_id="s1"):
        gate.mark_idle(987654)
        gate.mark_idle()                      # a token-less legacy release: non-human only
        assert gate.is_interactive_busy() and _marker(gate) is not None


def test_reused_pid_reads_as_stale(gate):
    mp = gate.turn_marker_path()
    os.makedirs(os.path.dirname(mp), exist_ok=True)
    from core.state.proc import process_identity
    me = process_identity()
    with open(mp, "w") as f:
        json.dump({"pid": os.getpid(), "pid_start": "not-" + me.start, "boot_id": me.boot,
                   "kind": "owner_chat", "started": 1.0, "session_id": "old"}, f)
    assert gate.read_turn_marker() is None


@pytest.mark.asyncio
async def test_asyncio_interleaved_turns(gate):
    seen = []
    ev_a, ev_b = asyncio.Event(), asyncio.Event()

    async def turn(name, wait_for, release):
        with gate.owner_turn(kind="owner_chat", session_id=name):
            seen.append(_marker(gate) is not None)
            release.set()
            await wait_for.wait()
            seen.append(_marker(gate) is not None)
        seen.append((name, _marker(gate) is not None))

    done_a = asyncio.Event()

    async def a():
        await turn("A", ev_b, ev_a)
        done_a.set()

    async def b():
        await ev_a.wait()
        with gate.owner_turn(kind="owner_chat", session_id="B"):
            ev_b.set()
            await done_a.wait()               # A leaves while B is live
            seen.append(_marker(gate) is not None)
    await asyncio.gather(a(), b())
    assert all(x if isinstance(x, bool) else True for x in seen)
    assert ("A", True) in seen                # B still live after A ended: marker kept
    assert _marker(gate) is None


def test_turn_gate_machine_well_formed(gate):
    assert gate.TURN_GATE.validate() == []
