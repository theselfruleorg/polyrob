"""Phase 6-B (path-concerns upgrade): cross-process workspace turn lock (C2).

The interactive busy-gate is process-global, so two `rob` processes in one dir
could mutate workspace files concurrently. A SafeFileLock on
<.rob>/workspace.turn.lock serializes them (blocking acquire, fail-loud on
timeout). Gated CLI_WORKSPACE_LOCK (default on) + POLYROB_WORKSPACE_LOCK_DIR (set by
build_cli_container). This is the audit's "expensive to be wrong about" 2-process
race test.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYROB_WORKSPACE_LOCK_DIR", raising=False)
    from core.interactive_gate import workspace_turn_lock

    with workspace_turn_lock():  # no dir configured -> no-op, must not raise
        pass


def test_contention_raises_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("CLI_WORKSPACE_LOCK", "1")
    from core.interactive_gate import workspace_turn_lock

    with workspace_turn_lock():
        # Second acquisition with timeout=0 must fail fast while the first is held.
        with pytest.raises(TimeoutError):
            with workspace_turn_lock(timeout=0):
                pass


def test_two_processes_serialize_workspace_writes(tmp_path):
    """Spawn two processes that each append markers under the lock; assert their
    critical sections do not interleave (each process's lines are contiguous)."""
    shared = tmp_path / "out.txt"
    lockdir = tmp_path
    driver = textwrap.dedent(
        f"""
        import os, sys, time
        os.environ["POLYROB_WORKSPACE_LOCK_DIR"] = {str(lockdir)!r}
        os.environ["CLI_WORKSPACE_LOCK"] = "1"
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from core.interactive_gate import workspace_turn_lock
        tag = sys.argv[1]
        with workspace_turn_lock(timeout=30):
            with open({str(shared)!r}, "a") as f:
                for i in range(5):
                    f.write(tag + str(i) + "\\n")
                    f.flush()
                    time.sleep(0.02)
        """
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", driver, tag])
        for tag in ("A", "B")
    ]
    for p in procs:
        assert p.wait(timeout=60) == 0

    lines = [ln for ln in shared.read_text().splitlines() if ln]
    assert len(lines) == 10
    # Each process wrote 5 contiguous lines; the two blocks must not interleave.
    tags = [ln[0] for ln in lines]
    # number of tag-transitions must be exactly 1 (A-block then B-block, or vice versa)
    transitions = sum(1 for i in range(1, len(tags)) if tags[i] != tags[i - 1])
    assert transitions == 1, f"writes interleaved: {tags}"
