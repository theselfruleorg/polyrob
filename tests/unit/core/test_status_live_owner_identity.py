"""098 review: the status live-now reader judges a registry row's owner the
way the registry does — by process identity, not by pid alone. A crashed
owner whose pid was reused kept the session "live" in /status and the
console's run_live for ever."""
import os
import sqlite3

from core.state.proc import ProcIdentity, process_identity
from core.status_live import session_is_live
from core.runtime_paths import session_registry_db_path


def _reg(tmp_path, rows):
    path = session_registry_db_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE active_sessions (session_id TEXT, worker_pid INTEGER, "
                  "owner_ident TEXT, created_at REAL, last_seen_at REAL)")
        c.executemany("INSERT INTO active_sessions VALUES (?,?,?,0,0)", rows)


def test_reused_pid_is_not_live(tmp_path):
    me = process_identity()
    ghost = ProcIdentity(me.pid, "some-older-start", me.boot).encode()
    _reg(tmp_path, [("s-ghost", me.pid, ghost), ("s-live", me.pid, me.encode()),
                    ("s-legacy", me.pid, None)])
    assert session_is_live("s-ghost", str(tmp_path)) is False
    assert session_is_live("s-live", str(tmp_path)) is True
    assert session_is_live("s-legacy", str(tmp_path)) is True


def test_a_naive_stamp_is_the_local_clock_not_utc(monkeypatch):
    """ST-20: cron's last_run_at and the registry's created_at are datetime.now()
    text; reading them as UTC put "running since" off by the UTC offset."""
    import time
    from datetime import datetime
    from core.status_live import _iso_ts
    monkeypatch.setenv("TZ", "Etc/GMT-5")  # UTC+5
    time.tzset()
    try:
        epoch = 1_800_000_000.0
        naive_local = datetime.fromtimestamp(epoch).isoformat()
        assert _iso_ts(naive_local) == epoch
        assert _iso_ts("2027-01-15T08:00:00+00:00") == 1_800_000_000.0
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
