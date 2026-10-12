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
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLI_WORKSPACE_LOCK", raising=False)
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
    from core.file_lock import SafeFileLock
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


def test_owner_turn_writes_the_marker_even_when_a_goal_run_holds_the_busy_depth(gate, caplog):
    """Prod 2026-09-19 10:36Z: the deploy waiter fired while the owner's 10:29 turn
    was at step 4 — no `turn.active` existed. The marker was only written when the
    busy depth was 0, so any other busy holder (a goal run, a leaked mark) made a
    genuine owner turn invisible to the waiter. The marker now follows its own
    turn depth, and every write/clear is logged at INFO so the next miss is
    diagnosable from the journal."""
    import logging
    gate.mark_busy()  # a goal run (or a leak) already holds the busy depth
    try:
        with caplog.at_level(logging.INFO, logger="core.interactive_gate"):
            with gate.owner_turn(kind="owner_chat", session_id="s9"):
                m = gate.read_turn_marker()
                assert m is not None and m["session_id"] == "s9"
            assert gate.read_turn_marker() is None
        assert any("turn marker written" in r.message for r in caplog.records)
        assert any("turn marker cleared" in r.message for r in caplog.records)
    finally:
        gate.mark_idle()


def test_nested_owner_turns_keep_one_marker(gate):
    with gate.owner_turn(kind="owner_chat", session_id="outer"):
        with gate.owner_turn(kind="owner_chat", session_id="inner"):
            assert gate.read_turn_marker()["session_id"] == "outer"
        assert gate.read_turn_marker() is not None  # inner exit must not clear it
    assert gate.read_turn_marker() is None


def test_marker_lives_under_data_dir_even_when_the_workspace_lock_dir_is_set(monkeypatch, tmp_path):
    """Prod 2026-09-19 18:29Z (and 10:36Z): the agent process carries
    POLYROB_WORKSPACE_LOCK_DIR=<project>/.polyrob (build_cli_container sets it), so
    the marker landed at <project>/.polyrob/turn.active — while the deploy waiter
    and `polyrob doctor` read <POLYROB_DATA_DIR>/locks/turn.active. Two locations,
    and a deploy fired at step 8 of a live owner turn. The marker is an
    OUT-OF-PROCESS fact: it lives under the data dir whenever one is known; the
    per-workspace lock file stays where it was."""
    from core import interactive_gate as g
    monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / "project" / ".polyrob"))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLI_WORKSPACE_LOCK", raising=False)
    assert g.turn_marker_path() == str(tmp_path / "data" / "locks" / "turn.active")
    assert g._workspace_lock_path() == str(tmp_path / "project" / ".polyrob" / "workspace.turn.lock")
    g._busy_depth = 0
    g._turn_depth = 0
    with g.owner_turn(kind="owner_chat", session_id="s1"):
        assert os.path.exists(tmp_path / "data" / "locks" / "turn.active")
        assert g.read_turn_marker()["session_id"] == "s1"
    assert not os.path.exists(tmp_path / "data" / "locks" / "turn.active")


def test_marker_follows_the_data_home_without_a_data_dir(monkeypatch, tmp_path):
    """077 F8: no POLYROB_DATA_DIR (the `polyrob service` user unit) — the marker
    resolves from the DATA HOME (<POLYROB_HOME>/data/locks), never from the lock
    dir (<WorkingDirectory>/.polyrob), so `polyrob update --apply` — which has no
    lock dir — reads the same file the gateway writes."""
    from core import interactive_gate as g
    monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / "cwd" / ".polyrob"))
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("CLI_WORKSPACE_LOCK", raising=False)
    from core.runtime_paths import resolve_data_home
    # (conftest points the local data home at the test's cwd; the RULE is what
    # this pins: the data home, not the lock dir.)
    assert g.turn_marker_path() == str(resolve_data_home() / "locks" / "turn.active")
    assert not g.turn_marker_path().startswith(str(tmp_path / "cwd"))


def test_no_marker_without_a_lock_dir_or_a_data_dir(monkeypatch, tmp_path):
    from core import interactive_gate as g
    monkeypatch.delenv("POLYROB_WORKSPACE_LOCK_DIR", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    assert g.turn_marker_path() is None


def test_writer_and_updater_agree_in_every_install_shape(monkeypatch, tmp_path):
    """077 F8 ratchet: the gateway (lock dir set by build_cli_container) writes a
    marker that `polyrob update`'s waiter (no lock dir) reads, in the prod shape
    (POLYROB_DATA_DIR) and the self-host shape (POLYROB_HOME only)."""
    from core import interactive_gate as g
    from cli.update.agent_state import observe
    from core.runtime_paths import resolve_data_home
    monkeypatch.delenv("CLI_WORKSPACE_LOCK", raising=False)
    for shape in ("prod", "selfhost"):
        monkeypatch.setenv("POLYROB_HOME", str(tmp_path / shape / "home"))
        if shape == "prod":
            monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / shape / "data"))
        else:
            monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
        # The gateway process.
        monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / shape / "cwd" / ".polyrob"))
        g._busy_depth = 0
        with g.owner_turn(kind="owner_chat", session_id="s1"):
            # The updater process: no lock dir.
            monkeypatch.delenv("POLYROB_WORKSPACE_LOCK_DIR", raising=False)
            act = observe(resolve_data_home())
            assert any("live human turn" in r for r in act.reasons), (shape, act.reasons)
            monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / shape / "cwd" / ".polyrob"))


def test_a_turn_never_clears_another_processs_marker(gate):
    """077 review: one marker per data home — a second process's live turn that
    rewrote it must not read as idle when this turn ends."""
    import json
    with gate.owner_turn(kind="owner_chat", session_id="s1"):
        with open(gate.turn_marker_path(), "w") as f:
            json.dump({"pid": os.getppid(), "kind": "owner_chat"}, f)
    assert os.path.exists(gate.turn_marker_path())
