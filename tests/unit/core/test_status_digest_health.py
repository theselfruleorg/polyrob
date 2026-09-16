"""C4 (2026-09-15 prod review): an enabled digest with no scheduled producer.

`user_delivery` records every suppressed owner message as an `owner_notice` on
the stated promise that it is "rolled into the digest". On prod
OWNER_DIGEST_ENABLED=true and CRON_DELIVERY_ENABLED=true, and the ONE cron job
carrying `payload.digest` was `cancelled` — so 889 suppressed messages had no
roll-up channel at all and `/missed` (last 5, 91% lifecycle chatter) was the
entire recovery surface. An enabled flag with no producer is the confident-zero
class: everything reports healthy and nothing runs.
"""
import json
import sqlite3

import pytest


def _cron_db(path, jobs):
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE cron_jobs (
        id TEXT PRIMARY KEY, user_id TEXT, task TEXT, spec TEXT, enabled INTEGER,
        status TEXT, next_run_at REAL, last_run_at REAL, payload TEXT)""")
    for j in jobs:
        conn.execute("INSERT INTO cron_jobs (id, user_id, task, spec, enabled, status,"
                     " next_run_at, last_run_at, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                     (j["id"], "rob", j.get("task", "t"), "1d", j.get("enabled", 1),
                      j.get("status", "scheduled"), 0, 0, json.dumps(j.get("payload", {}))))
    conn.commit()
    conn.close()


def _health_keys(tmp_path):
    from core.status_snapshot import _digest_health
    return [h.key for h in _digest_health("rob", str(tmp_path))]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)


def test_enabled_digest_with_no_job_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setenv("OWNER_DIGEST_ENABLED", "true")
    _cron_db(tmp_path / "cron.db", [{"id": "a", "payload": {}}])
    assert "digest_unscheduled" in _health_keys(tmp_path)


def test_a_cancelled_digest_job_does_not_count(tmp_path, monkeypatch):
    """Exactly prod's shape: the digest job exists but is cancelled."""
    monkeypatch.setenv("OWNER_DIGEST_ENABLED", "true")
    _cron_db(tmp_path / "cron.db", [
        {"id": "d", "status": "cancelled", "enabled": 0, "payload": {"digest": 1}}])
    assert "digest_unscheduled" in _health_keys(tmp_path)


def test_a_live_digest_job_is_healthy(tmp_path, monkeypatch):
    monkeypatch.setenv("OWNER_DIGEST_ENABLED", "true")
    _cron_db(tmp_path / "cron.db", [
        {"id": "d", "status": "scheduled", "enabled": 1, "payload": {"digest": 1}}])
    assert _health_keys(tmp_path) == []


def test_a_disabled_digest_is_not_flagged(tmp_path, monkeypatch):
    monkeypatch.setenv("OWNER_DIGEST_ENABLED", "false")
    _cron_db(tmp_path / "cron.db", [{"id": "a", "payload": {}}])
    assert _health_keys(tmp_path) == []


def test_an_unreadable_cron_store_is_not_a_confident_zero(tmp_path, monkeypatch):
    """No cron.db at all: we cannot say the digest is scheduled OR unscheduled.
    Report the fact rather than a clean bill of health."""
    monkeypatch.setenv("OWNER_DIGEST_ENABLED", "true")
    assert "digest_unknown" in _health_keys(tmp_path)
