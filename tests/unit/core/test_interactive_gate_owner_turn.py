"""056 WS3 — an owner turn on a headless surface holds the shared workspace.

Prod 2026-09-19: `interactive_gate.mark_busy` was called only by the REPL and by
goal runs, so on the Telegram-headless server an owner chat turn never marked the
workspace busy — the 01:00 EXIT rail and the owner's 02:18 outreach turn ran
concurrently under the same `project/` root (both writing). `owner_turn()` closes
that: busy depth + the cross-process lock (non-blocking: an owner turn is never
refused, it proceeds without the lock and says so) + a `turn.active` marker the
deploy waiter and the status snapshot can read out-of-process.
"""
import json
import os

import pytest


@pytest.fixture
def gate(monkeypatch, tmp_path):
    from core import interactive_gate as g
    monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("CLI_WORKSPACE_LOCK", raising=False)
    monkeypatch.delenv("INTERACTIVE_GATE_MARKER", raising=False)
    g._busy_depth = 0
    return g


def test_owner_turn_marks_busy_and_writes_marker(gate, tmp_path):
    assert not gate.is_interactive_busy()
    with gate.owner_turn(kind="owner_chat", session_id="s1"):
        assert gate.is_interactive_busy()
        m = gate.read_turn_marker()
        assert m and m["pid"] == os.getpid() and m["kind"] == "owner_chat"
        assert m["session_id"] == "s1" and m["started"] > 0
        assert os.path.exists(gate.turn_marker_path())
    assert not gate.is_interactive_busy()
    assert gate.read_turn_marker() is None
    assert not os.path.exists(gate.turn_marker_path())


def test_owner_turn_clears_marker_when_the_turn_raises(gate):
    with pytest.raises(RuntimeError):
        with gate.owner_turn(kind="owner_chat", session_id="s1"):
            raise RuntimeError("boom")
    assert not gate.is_interactive_busy()
    assert gate.read_turn_marker() is None


def test_stale_marker_from_a_dead_pid_reads_as_none(gate, tmp_path):
    os.makedirs(os.path.dirname(gate.turn_marker_path()), exist_ok=True)
    with open(gate.turn_marker_path(), "w") as f:
        json.dump({"pid": 2**22 + 12345, "kind": "owner_chat", "started": 1.0,
                   "session_id": "old"}, f)
    assert gate.read_turn_marker() is None, "a marker from a dead process is not a turn"


def test_owner_turn_never_refuses_when_lock_is_held(gate, tmp_path):
    """Another process holding the workspace lock must not block the owner: the
    turn proceeds without the lock (fail-open for the human, logged)."""
    from agents.task.utils import SafeFileLock
    lp = gate._workspace_lock_path()
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    held = SafeFileLock(lp, timeout=1)
    held.__enter__()
    try:
        with gate.owner_turn(kind="owner_chat", session_id="s2", lock_timeout=0.2):
            assert gate.is_interactive_busy()
    finally:
        held.__exit__(None, None, None)


def test_server_gets_a_lock_dir_from_data_dir(monkeypatch, tmp_path):
    """No POLYROB_WORKSPACE_LOCK_DIR (the headless server) → derive <data>/locks
    so cron/goal ticks and the deploy waiter see the same lock the REPL uses."""
    from core import interactive_gate as g
    monkeypatch.delenv("POLYROB_WORKSPACE_LOCK_DIR", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    p = g._workspace_lock_path()
    assert p and p.startswith(str(tmp_path / "data")) and p.endswith("workspace.turn.lock")
