"""SafeFileLock never steals a held lock because its file is old (EP-1).

A flock-backed lock file's mtime does not move while its holder keeps it, so the
old "older than 5 min = stale, unlink it" rule let a second process lock a new
inode beside a live holder (a slow `polyrob update` lost its update.lock). Only an
O_EXCL fallback lock whose recorded pid is dead may be cleared.
"""
import os
import subprocess
import sys
import time

import pytest

from core.file_lock import SafeFileLock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_SECOND = """
import os, sys, time
import filelock
from core.file_lock import SafeFileLock

# Age the HELD lock file at the moment this contender's flock attempt times out, so
# the test does not depend on whether the filelock version refreshes the mtime when
# it opens the file (3.x opens with O_TRUNC; 4.x does not).
_orig = filelock.FileLock.acquire
def _acquire(self, *a, **k):
    try:
        return _orig(self, *a, **k)
    except filelock.Timeout:
        old = time.time() - 4000
        os.utime(sys.argv[1], (old, old))
        raise
filelock.FileLock.acquire = _acquire

b = SafeFileLock(sys.argv[1], timeout=0)
try:
    b.__enter__()
    print("ACQUIRED")
except Exception as e:
    print("REFUSED", type(e).__name__)
"""


def _age(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_old_flock_held_lock_is_not_stolen_by_another_process(tmp_path):
    pytest.importorskip("filelock")
    path = str(tmp_path / "update.lock")
    held = SafeFileLock(path, timeout=0)
    held.__enter__()
    try:
        _age(path, 400)  # a holder that has kept the lock for more than 5 min
        out = subprocess.run([sys.executable, "-c", _SECOND, path], capture_output=True,
                             text=True, cwd=_ROOT).stdout.strip()
        assert out == "REFUSED TimeoutError", out
        assert os.path.exists(path)
    finally:
        held.__exit__(None, None, None)


def test_flock_timeout_raises_and_never_falls_back(tmp_path, monkeypatch):
    """A filelock Timeout means the flock is HELD: raise, never try the O_EXCL fallback
    on the same (empty, old) file."""
    filelock = pytest.importorskip("filelock")
    path = str(tmp_path / "owner_turn.lock")
    open(path, "w").close()
    _age(path, 4000)
    lock = SafeFileLock(path, timeout=0)

    def _held(*a, **k):
        raise filelock.Timeout(path)

    monkeypatch.setattr(lock._filelock, "acquire", _held)
    with pytest.raises(TimeoutError, match="Failed to acquire lock"):
        lock.__enter__()
    assert lock._use_filelock is True
    assert os.path.exists(path)


def test_flock_error_fallback_never_clears_the_empty_flock_file(tmp_path, monkeypatch):
    """Any other filelock error switches to the fallback, but the empty file the flock
    path made is not a dead fallback lock, however old it is."""
    pytest.importorskip("filelock")
    path = str(tmp_path / "owner_turn.lock")
    open(path, "w").close()
    _age(path, 4000)
    lock = SafeFileLock(path, timeout=0)

    def _broken(*a, **k):
        raise RuntimeError("flock unsupported here")

    monkeypatch.setattr(lock._filelock, "acquire", _broken)
    with pytest.raises(TimeoutError):
        lock.__enter__()
    assert os.path.exists(path)


def test_flock_lock_left_by_a_dead_holder_is_free(tmp_path):
    pytest.importorskip("filelock")
    path = str(tmp_path / "update.lock")
    open(path, "w").close()  # a crashed holder leaves the file; the kernel freed the flock
    _age(path, 4000)
    lock = SafeFileLock(path, timeout=0)
    with lock:
        assert lock._locked is True


def _fallback(path, timeout=0):
    lock = SafeFileLock(path, timeout=timeout)
    lock._use_filelock = False
    return lock


def test_fallback_lock_of_a_live_pid_is_kept_however_old(tmp_path):
    path = str(tmp_path / "held.lock")
    with open(path, "w") as fh:
        fh.write(f"{os.getppid()}\n")
    _age(path, 4000)
    with pytest.raises(TimeoutError):
        _fallback(path).__enter__()
    assert os.path.exists(path)


def test_fallback_lock_of_a_dead_pid_is_cleared_and_taken(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    path = str(tmp_path / "crashed.lock")
    with open(path, "w") as fh:
        fh.write(f"{proc.pid}\n")
    lock = _fallback(path)
    with lock:
        assert lock._locked is True
    assert not os.path.exists(path)


def test_cleanup_never_unlinks_a_lock_this_process_holds(tmp_path):
    path = str(tmp_path / "mine.lock")
    lock = _fallback(path)
    lock.__enter__()
    try:
        _age(path, 4000)
        SafeFileLock._last_cleanup = 0
        SafeFileLock._maybe_cleanup_stale_locks()
        assert os.path.exists(path)
    finally:
        lock.__exit__(None, None, None)


def test_fallback_lock_records_the_process_identity(tmp_path):
    from core.state.proc import process_identity
    path = str(tmp_path / "mine.lock")
    lock = _fallback(path)
    with lock:
        with open(path) as fh:
            assert fh.read().strip() == process_identity().encode()
    assert not os.path.exists(path)


def test_fallback_lock_of_a_reused_pid_after_reboot_is_cleared(tmp_path):
    """The recorded pid runs again (reused after a reboot): its boot id differs."""
    from core.state.proc import ProcIdentity
    path = str(tmp_path / "reused.lock")
    with open(path, "w") as fh:
        fh.write(ProcIdentity(os.getppid(), "Mon Jan  1 00:00:00 2024", "boot-earlier").encode() + "\n")
    lock = _fallback(path)
    with lock:
        assert lock._locked is True


def test_fallback_lock_of_a_reused_pid_same_boot_is_cleared(tmp_path):
    from core.state.proc import ProcIdentity, boot_id
    path = str(tmp_path / "reused.lock")
    with open(path, "w") as fh:
        fh.write(ProcIdentity(os.getppid(), "not-its-start-time", boot_id()).encode() + "\n")
    lock = _fallback(path)
    with lock:
        assert lock._locked is True


def test_fallback_lock_of_a_live_identity_is_kept(tmp_path):
    from core.state.proc import ProcIdentity, boot_id, start_time
    ppid = os.getppid()
    path = str(tmp_path / "held.lock")
    with open(path, "w") as fh:
        fh.write(ProcIdentity(ppid, start_time(ppid), boot_id()).encode() + "\n")
    _age(path, 4000)
    with pytest.raises(TimeoutError):
        _fallback(path).__enter__()
    assert os.path.exists(path)
