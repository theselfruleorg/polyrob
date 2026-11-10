"""workspace_lock_busy probes the turn lock with SafeFileLock's context protocol.

The probe called ``acquire()``/``release()``, which SafeFileLock does not have, so the
AttributeError read as "held" whenever workspace.turn.lock EXISTED — and lock files are
never deleted, so every update waited on a lock nobody held.
"""
import os
import subprocess
import sys

import pytest

from cli.update import process_guard
from cli.update.process_guard import active_use_reasons, workspace_lock_busy
from core.file_lock import SafeFileLock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))))


def test_no_lock_file_is_free(tmp_path):
    assert workspace_lock_busy(tmp_path) is False


def test_existing_but_free_lock_file_is_not_busy(tmp_path):
    lp = tmp_path / "workspace.turn.lock"
    with SafeFileLock(str(lp), timeout=0):
        pass
    open(lp, "a").close()  # the file stays behind after a release
    assert workspace_lock_busy(tmp_path) is False


def test_lock_held_by_another_process_is_busy(tmp_path):
    pytest.importorskip("filelock")
    lp = str(tmp_path / "workspace.turn.lock")
    code = ("import sys,time\nfrom core.file_lock import SafeFileLock\n"
            "l=SafeFileLock(sys.argv[1], timeout=0); l.__enter__(); print('HELD', flush=True); "
            "time.sleep(30)")
    proc = subprocess.Popen([sys.executable, "-c", code, lp], cwd=_ROOT,
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "HELD"
        assert workspace_lock_busy(tmp_path) is True
    finally:
        proc.kill()
        proc.wait()


def test_an_error_other_than_held_is_raised_not_reported_as_held(tmp_path, monkeypatch):
    open(tmp_path / "workspace.turn.lock", "w").close()

    class _Broken:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise RuntimeError("Failed to acquire lock: permission denied")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("core.file_lock.SafeFileLock", _Broken)
    with pytest.raises(RuntimeError):
        workspace_lock_busy(tmp_path)
    # The restore guard names it: an unknown lock state is not "safe".
    monkeypatch.setattr(process_guard, "server_process_alive", lambda: False)
    reasons = active_use_reasons([], lock_dir=tmp_path)
    assert any("could not be checked" in r for r in reasons), reasons
