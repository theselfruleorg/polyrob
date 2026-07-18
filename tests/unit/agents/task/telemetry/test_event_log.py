"""Durable telemetry event log (telemetry audit 2026-07-04, Phase 2 foundation).

The autonomy loops + governance surface emit to bare logs or in-memory lists that
vanish on restart, and nothing gives an operator a cross-session view. This is a
small append-only SQLite sink: record(kind, ...) + query/aggregate, tenant-scoped,
fail-open. It's the durable layer the fleet API and emitters build on.
"""
import pytest

from agents.task.telemetry.event_log import TelemetryEventLog


def _log(tmp_path):
    return TelemetryEventLog(str(tmp_path / "telemetry_events.db"))


def test_record_and_query_roundtrip(tmp_path):
    log = _log(tmp_path)
    log.record("cron_run", user_id="u1", session_id="s1", source="cron",
               outcome="done", duration_s=1.5, ts=100.0)
    rows = log.query()
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "cron_run"
    assert r["user_id"] == "u1"
    assert r["attrs"]["outcome"] == "done"
    assert r["attrs"]["duration_s"] == 1.5


def test_query_filters_by_kind_and_since_and_user(tmp_path):
    log = _log(tmp_path)
    log.record("cron_run", user_id="u1", ts=100.0, outcome="done")
    log.record("wallet_spend", user_id="u1", ts=200.0, amount_usd=5.0)
    log.record("cron_run", user_id="u2", ts=300.0, outcome="failed")

    assert len(log.query(kind="cron_run")) == 2
    assert len(log.query(user_id="u1")) == 2
    assert len(log.query(since_ts=250.0)) == 1
    assert log.query(kind="wallet_spend")[0]["attrs"]["amount_usd"] == 5.0


def test_aggregate_counts_and_spend(tmp_path):
    log = _log(tmp_path)
    log.record("cron_run", user_id="u1", ts=100.0, outcome="done")
    log.record("cron_run", user_id="u1", ts=110.0, outcome="failed")
    log.record("wallet_spend", user_id="u1", ts=120.0, amount_usd=3.0)
    log.record("wallet_spend", user_id="u1", ts=130.0, amount_usd=2.5)

    agg = log.aggregate()
    assert agg["counts_by_kind"]["cron_run"] == 2
    assert agg["counts_by_kind"]["wallet_spend"] == 2
    assert abs(agg["wallet_spend_usd"] - 5.5) < 1e-9
    assert "total_spend_usd" not in agg


def test_record_is_fail_open_on_bad_db(tmp_path):
    log = TelemetryEventLog("/nonexistent_dir_zzz/telemetry_events.db")
    # Must not raise even though the DB can't be opened/created.
    log.record("cron_run", user_id="u1")
    assert log.query() == []


def test_prune_removes_old_rows(tmp_path):
    log = _log(tmp_path)
    log.record("tick", ts=100.0)
    log.record("tick", ts=200.0)
    log.record("tick", ts=300.0)
    removed = log.prune(older_than_ts=250.0)
    assert removed == 2
    remaining = log.query()
    assert len(remaining) == 1
    assert remaining[0]["ts"] == 300.0


def test_query_limit_and_desc_order(tmp_path):
    log = _log(tmp_path)
    for i in range(5):
        log.record("tick", ts=float(i))
    rows = log.query(limit=2)
    # Most recent first.
    assert [r["attrs"].get("_none", r["ts"]) for r in rows] == [4.0, 3.0]


def test_get_event_log_honors_env_path_override(tmp_path, monkeypatch):
    """TELEMETRY_EVENT_LOG_PATH redirects the default singleton — the seam the
    test suite uses to keep durable telemetry OUT of the developer's data home
    (and the §3.2 delivery-rail memory isolated per test)."""
    from agents.task.telemetry.event_log import get_event_log
    p = str(tmp_path / "redirected.db")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", p)
    log = get_event_log()
    assert log.db_path == p
