"""SafeFileLock fallback (no `filelock` package) must acquire a FREE lock even with
timeout=0, and still fail fast when the lock is genuinely contended. Regression for the
goal-dispatcher/cron gate that silently never ran because workspace_turn_lock(timeout=0)
could not acquire a free lock on machines without filelock."""
import os

import pytest

from agents.task.utils import SafeFileLock


def test_timeout_zero_acquires_free_lock(tmp_path):
    lock_file = str(tmp_path / "free.lock")
    lock = SafeFileLock(lock_file, timeout=0)
    lock._use_filelock = False  # force the manual fallback path under test
    with lock:
        assert lock._locked is True
        assert os.path.exists(lock_file)
    # released on exit
    assert not os.path.exists(lock_file)


def test_timeout_zero_fails_fast_when_contended(tmp_path):
    lock_file = str(tmp_path / "held.lock")
    # Simulate contention: the lock file already exists (held by another holder).
    with open(lock_file, "w") as fh:
        fh.write("99999\n")
    lock = SafeFileLock(lock_file, timeout=0)
    lock._use_filelock = False
    with pytest.raises(TimeoutError):
        lock.__enter__()
