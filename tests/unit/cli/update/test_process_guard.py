"""`cli/update/process_guard.py` — refuse a destructive restore while a live process
holds the DBs, and serialize concurrent update/rollback runs.

restore_snapshot() os.replace's DB files and deletes -wal/-shm. Doing that under a
live WAL writer corrupts state. The guard is best-effort (an idle agent holds no
write lock) but must reliably catch an ACTIVE writer.
"""
import sqlite3
import threading
import time

import pytest

from cli.update.process_guard import (
    UpdateLockHeld, active_use_reasons, dbs_in_use, server_process_alive,
    update_lock,
)


def _seed(path, rows=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
    c.executemany("INSERT INTO t DEFAULT VALUES", [() for _ in range(rows)])
    c.commit()
    c.close()


def test_idle_db_not_in_use(tmp_path):
    db = tmp_path / "memory.db"
    _seed(db)
    assert dbs_in_use([db]) == []
    assert active_use_reasons([db]) == []


def test_missing_db_not_in_use(tmp_path):
    assert dbs_in_use([tmp_path / "nope.db"]) == []


def test_active_writer_is_detected(tmp_path):
    db = tmp_path / "goals.db"
    _seed(db)
    holder = sqlite3.connect(str(db))
    try:
        holder.execute("PRAGMA busy_timeout=0")
        holder.execute("BEGIN IMMEDIATE")  # hold the write lock
        in_use = dbs_in_use([db])
        assert db.resolve() in [p.resolve() for p in in_use]
        assert any("goals.db" in r for r in active_use_reasons([db]))
    finally:
        holder.rollback()
        holder.close()


def test_active_writer_across_thread_is_detected(tmp_path):
    db = tmp_path / "cron.db"
    _seed(db)
    holding = threading.Event()
    release = threading.Event()

    def hold():
        c = sqlite3.connect(str(db))
        c.execute("PRAGMA busy_timeout=0")
        c.execute("BEGIN IMMEDIATE")
        holding.set()
        release.wait(5)
        c.rollback()
        c.close()

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holding.wait(5)
        assert dbs_in_use([db])
    finally:
        release.set()
        t.join(5)


# --- server-process detection (prod-test-driven: an idle-but-running server holds
# no DB lock and opens DBs on demand, so only detecting the PROCESS is safe) ------

def test_server_process_detected_from_cmdline():
    # The real prod signature: `.../venv/bin/polyrob telegram`.
    cmdlines = [(999, ["/opt/polyrob/venv/bin/python3",
                       "/opt/polyrob/venv/bin/polyrob", "telegram"])]
    assert server_process_alive(exclude_pid=1, _cmdlines=cmdlines) is True


def test_uvicorn_api_server_detected():
    cmdlines = [(42, ["python", "-m", "uvicorn", "api.app:app"])]
    assert server_process_alive(exclude_pid=1, _cmdlines=cmdlines) is True


def test_own_update_process_not_matched():
    # A sibling `polyrob update --rollback` must NOT count as a running server.
    cmdlines = [(7, ["/opt/polyrob/venv/bin/polyrob", "update", "--rollback"])]
    assert server_process_alive(exclude_pid=1, _cmdlines=cmdlines) is False


def test_self_pid_excluded():
    cmdlines = [(1234, ["polyrob", "telegram"])]
    assert server_process_alive(exclude_pid=1234, _cmdlines=cmdlines) is False


def test_unrelated_process_not_matched():
    cmdlines = [(5, ["python", "some_other_app.py", "serve"])]
    assert server_process_alive(exclude_pid=1, _cmdlines=cmdlines) is False


def test_active_use_reasons_flags_running_server(monkeypatch):
    import cli.update.process_guard as pg
    monkeypatch.setattr(pg, "server_process_alive", lambda **k: True)
    reasons = active_use_reasons([])
    assert any("server" in r.lower() or "running" in r.lower() for r in reasons)


# ---------------------------------------------------------------------------
# U8 (2026-07-14 review): the process scan must not be /proc-only — on macOS
# (no /proc) the in-use guard was fail-open and --apply/--rollback would
# os.replace DBs under a live agent without --force.
# ---------------------------------------------------------------------------

def test_cmdline_scan_not_empty_on_this_platform():
    """The default scan yields processes on EVERY supported platform (this test
    runs on macOS in dev and Linux in CI — both must produce a non-empty scan)."""
    from cli.update.process_guard import _iter_cmdlines
    assert any(True for _ in _iter_cmdlines()), (
        "process scan is empty — the in-use guard is fail-open on this platform")


def test_ps_output_parsing():
    from cli.update.process_guard import _parse_ps_lines
    out = (
        "  123 /opt/polyrob/venv/bin/polyrob telegram\n"
        "  456 python -m uvicorn api.app:app\n"
        "notapid junk line\n"
        "\n"
    )
    parsed = list(_parse_ps_lines(out))
    assert (123, ["/opt/polyrob/venv/bin/polyrob", "telegram"]) in parsed
    assert (456, ["python", "-m", "uvicorn", "api.app:app"]) in parsed
    assert len(parsed) == 2


def test_live_scan_detects_spawned_polyrob_lookalike(tmp_path):
    """End-to-end on the real platform scanner: spawn a process whose argv looks
    like the prod agent (`polyrob telegram`) and require the guard to see it."""
    import subprocess
    import sys
    import time as _time

    from cli.update.process_guard import server_process_alive

    fake = tmp_path / "polyrob"
    fake.write_text("import time\ntime.sleep(30)\n")
    proc = subprocess.Popen([sys.executable, str(fake), "telegram"])
    try:
        _time.sleep(0.3)  # let the scanner see it
        assert server_process_alive() is True
    finally:
        proc.kill()
        proc.wait()


def test_update_lock_serializes(tmp_path):
    with update_lock(tmp_path):
        with pytest.raises(UpdateLockHeld):
            with update_lock(tmp_path):
                pass


def test_update_lock_releases(tmp_path):
    with update_lock(tmp_path):
        pass
    # After release, re-acquire must succeed.
    with update_lock(tmp_path):
        pass
