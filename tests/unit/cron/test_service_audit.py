"""A29 (043) — CronService.schedule/cancel emit a service-level audit event.

Every cron seat (console, CLI, Telegram) schedules/cancels through this ONE
service, so recording the domain event HERE audits all three at once — naming
the actor (``user_id``) and the caller's ``via`` surface. This is distinct from
the console's own ``console_write(CONSOLE_CRON_CANCEL)`` (the console-action
audit); a dual record is intended, like J1's app-kind pair.
"""
from datetime import datetime

import pytest

from core.event_kinds import CRON_CANCELLED, CRON_SCHEDULED
from cron.jobs import CronJobStore
from cron.service import CronService


def _svc(tmp_path, now="2026-06-06T12:00:00"):
    store = CronJobStore(str(tmp_path / "cron.db"))
    return CronService(store, now=lambda: datetime.fromisoformat(now)), store


@pytest.fixture
def event_log(tmp_path, monkeypatch):
    """Isolated durable event log wired under the lazy import in _audit."""
    import core.event_log as el
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    return log


def test_schedule_records_actor_and_via(tmp_path, event_log):
    svc, _ = _svc(tmp_path)
    job = svc.schedule(task="ping", schedule_spec="1h", user_id="u1", via="cli")

    rows = event_log.query(kind=CRON_SCHEDULED)
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == "u1"          # the actor
    assert row["attrs"]["via"] == "cli"    # the surface
    assert row["attrs"]["job_id"] == job.id
    assert row["source"] == "cron"


def test_cancel_records_actor_and_via(tmp_path, event_log):
    svc, _ = _svc(tmp_path)
    job = svc.schedule(task="ping", schedule_spec="1h", user_id="u1", via="cli")
    assert svc.cancel(job.id, user_id="u1", via="telegram") is True

    rows = event_log.query(kind=CRON_CANCELLED)
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == "u1"
    assert row["attrs"]["via"] == "telegram"
    assert row["attrs"]["job_id"] == job.id
    assert row["source"] == "cron"


def test_via_defaults_to_empty_but_still_audits(tmp_path, event_log):
    # A caller that does not name a surface (e.g. the console cancel today) is
    # still audited — the domain event is never silent.
    svc, _ = _svc(tmp_path)
    job = svc.schedule(task="ping", schedule_spec="1h", user_id="u2")
    assert event_log.query(kind=CRON_SCHEDULED)[0]["attrs"]["via"] == ""
    svc.cancel(job.id, user_id="u2")
    assert event_log.query(kind=CRON_CANCELLED)[0]["attrs"]["via"] == ""


def test_noop_cancel_records_nothing(tmp_path, event_log):
    # A cancel that changed no state (missing / wrong tenant) is not a domain
    # event — only a real cancel is recorded.
    svc, _ = _svc(tmp_path)
    assert svc.cancel("does-not-exist", user_id="u1") is False
    assert event_log.query(kind=CRON_CANCELLED) == []


def test_audit_is_fail_open(tmp_path, monkeypatch):
    # A broken event log must never break the schedule/cancel it observes.
    import core.event_log as el

    class Boom:
        def record(self, *a, **k):
            raise RuntimeError("sink down")

    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: Boom())
    svc, _ = _svc(tmp_path)
    # Neither call raises despite the sink raising.
    job = svc.schedule(task="ping", schedule_spec="1h", user_id="u1", via="cli")
    assert svc.cancel(job.id, user_id="u1", via="cli") is True


def test_audit_respects_disabled_flag(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "off")
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    svc, _ = _svc(tmp_path)
    job = svc.schedule(task="ping", schedule_spec="1h", user_id="u1", via="cli")
    svc.cancel(job.id, user_id="u1", via="cli")
    assert log.query(kind=CRON_SCHEDULED) == []
    assert log.query(kind=CRON_CANCELLED) == []
