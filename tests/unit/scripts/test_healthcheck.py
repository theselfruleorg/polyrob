import sqlite3
import subprocess
import time

from scripts.healthcheck import (
    check_goals_db,
    check_watchdog_liveness,
    _cooldown_elapsed,
    _touch,
    send_owner_alert,
)


def test_check_goals_db_ok(tmp_path):
    db = tmp_path / "goals.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE goals (id TEXT)")
    con.commit()
    con.close()
    ok, msg = check_goals_db(str(tmp_path))
    assert ok is True
    assert "goals.db" in msg


def test_check_goals_db_missing(tmp_path):
    ok, msg = check_goals_db(str(tmp_path))
    assert ok is False


# ---------------------------------------------------------------------------
# 2026-08-28: watchdog-liveness check (independent backstop, HIGH inbox item)
# ---------------------------------------------------------------------------

def _fake_run(recent_offset_sec, *, exit_ts="Fri 2026-08-28 18:50:30 UTC",
              start_ts="Fri 2026-08-28 18:50:29 UTC"):
    """Build a fake subprocess.run that answers `systemctl show` with the
    EXIT and START timestamps (two lines, matching real --value output for
    two -p flags), then `date -d ... +%s` with the real epoch for whichever
    one the code chose — mirrors the real two-subprocess-call shape without
    touching the actual system clock format parsing."""
    epoch = int(time.time() - recent_offset_sec)

    def _run(cmd, **kw):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{exit_ts}\n{start_ts}\n", stderr="")
        if cmd[0] == "date":
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{epoch}\n", stderr="")
        raise AssertionError(f"unexpected command {cmd}")
    return _run


def test_watchdog_liveness_ok_within_window(monkeypatch):
    monkeypatch.setattr("scripts.healthcheck.subprocess.run", _fake_run(60))
    ok, msg = check_watchdog_liveness("polyrob-maint-watchdog.service", 600, slack=3)
    assert ok is True
    assert "ok" in msg


def test_watchdog_liveness_stale_past_slack(monkeypatch):
    monkeypatch.setattr("scripts.healthcheck.subprocess.run", _fake_run(3601))
    ok, msg = check_watchdog_liveness("polyrob-maint-watchdog.service", 600, slack=3)
    assert ok is False
    assert "STALE" in msg


def test_watchdog_liveness_never_run(monkeypatch):
    def _run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="n/a\nn/a\n", stderr="")
    monkeypatch.setattr("scripts.healthcheck.subprocess.run", _run)
    ok, msg = check_watchdog_liveness("polyrob-maint-watchdog.service", 600, slack=3)
    assert ok is False
    assert "never run" in msg


def test_watchdog_liveness_falls_back_to_start_timestamp_mid_run(monkeypatch):
    """2026-08-28 live race: querying right as a oneshot fires can read
    ExecMainExitTimestamp as empty even though the service is actively
    running — the START timestamp must still count as "alive"."""
    monkeypatch.setattr(
        "scripts.healthcheck.subprocess.run",
        _fake_run(30, exit_ts="n/a", start_ts="Fri 2026-08-28 18:50:30 UTC"))
    ok, msg = check_watchdog_liveness("polyrob-maint-watchdog.service", 600, slack=3)
    assert ok is True
    assert "ok" in msg


def test_watchdog_liveness_probe_error_fails_closed(monkeypatch):
    def _run(cmd, **kw):
        raise OSError("systemctl not found")
    monkeypatch.setattr("scripts.healthcheck.subprocess.run", _run)
    ok, msg = check_watchdog_liveness("polyrob-maint-watchdog.service", 600, slack=3)
    assert ok is False
    assert "error" in msg


def test_cooldown_elapsed_true_when_no_file(tmp_path):
    assert _cooldown_elapsed(str(tmp_path / "nope"), 3600) is True


def test_cooldown_elapsed_false_right_after_touch(tmp_path):
    p = tmp_path / "marker"
    _touch(str(p))
    assert _cooldown_elapsed(str(p), 3600) is False


def test_cooldown_elapsed_true_after_window_passes(tmp_path):
    import os
    p = tmp_path / "marker"
    _touch(str(p))
    old = time.time() - 7200
    os.utime(str(p), (old, old))
    assert _cooldown_elapsed(str(p), 3600) is True


def test_send_owner_alert_calls_ops_alert_subprocess(monkeypatch):
    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr("scripts.healthcheck.subprocess.run", _run)
    send_owner_alert("test message")
    assert calls
    assert calls[0][-1] == "test message"
    assert calls[0][1].endswith("ops_alert.py")


def test_send_owner_alert_fails_open(monkeypatch):
    def _run(cmd, **kw):
        raise OSError("boom")
    monkeypatch.setattr("scripts.healthcheck.subprocess.run", _run)
    send_owner_alert("test message")  # must not raise


# --- main() integration: alerts only past cooldown, respects --no-alert ----

def test_main_alerts_on_stale_watchdog_once_then_respects_cooldown(tmp_path, monkeypatch):
    from scripts import healthcheck

    def _process_ok(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="12345\n", stderr="")

    monkeypatch.setattr(healthcheck, "check_process", lambda pattern: (True, "process up"))
    monkeypatch.setattr(healthcheck, "check_goals_db", lambda data_dir: (True, "goals.db reachable"))
    monkeypatch.setattr(healthcheck, "check_watchdog_liveness",
                        lambda service, interval, slack: (False, f"{service} STALE"))
    alerts = []
    monkeypatch.setattr(healthcheck, "send_owner_alert", lambda msg: alerts.append(msg))

    rc1 = healthcheck.main(["--data-dir", str(tmp_path)])
    assert rc1 == 1
    assert len(alerts) == len(healthcheck.WATCHDOG_SERVICES)  # one per stale service

    # Second run within the cooldown window: no new alerts.
    rc2 = healthcheck.main(["--data-dir", str(tmp_path)])
    assert rc2 == 1
    assert len(alerts) == len(healthcheck.WATCHDOG_SERVICES)  # unchanged


def test_main_no_alert_flag_suppresses_alerting(tmp_path, monkeypatch):
    from scripts import healthcheck
    monkeypatch.setattr(healthcheck, "check_process", lambda pattern: (True, "process up"))
    monkeypatch.setattr(healthcheck, "check_goals_db", lambda data_dir: (True, "goals.db reachable"))
    monkeypatch.setattr(healthcheck, "check_watchdog_liveness",
                        lambda service, interval, slack: (False, f"{service} STALE"))
    alerts = []
    monkeypatch.setattr(healthcheck, "send_owner_alert", lambda msg: alerts.append(msg))

    rc = healthcheck.main(["--data-dir", str(tmp_path), "--no-alert"])
    assert rc == 1
    assert alerts == []


def test_main_skip_watchdog_flag_omits_the_check(tmp_path, monkeypatch):
    from scripts import healthcheck
    monkeypatch.setattr(healthcheck, "check_process", lambda pattern: (True, "process up"))
    monkeypatch.setattr(healthcheck, "check_goals_db", lambda data_dir: (True, "goals.db reachable"))
    called = []
    monkeypatch.setattr(healthcheck, "check_watchdog_liveness",
                        lambda *a, **k: called.append(1) or (True, "ok"))

    rc = healthcheck.main(["--data-dir", str(tmp_path), "--skip-watchdog"])
    assert rc == 0
    assert called == []


def test_main_healthy_when_watchdogs_ok(tmp_path, monkeypatch):
    from scripts import healthcheck
    monkeypatch.setattr(healthcheck, "check_process", lambda pattern: (True, "process up"))
    monkeypatch.setattr(healthcheck, "check_goals_db", lambda data_dir: (True, "goals.db reachable"))
    monkeypatch.setattr(healthcheck, "check_watchdog_liveness",
                        lambda service, interval, slack: (True, f"{service} ok"))
    alerts = []
    monkeypatch.setattr(healthcheck, "send_owner_alert", lambda msg: alerts.append(msg))

    rc = healthcheck.main(["--data-dir", str(tmp_path)])
    assert rc == 0
    assert alerts == []
