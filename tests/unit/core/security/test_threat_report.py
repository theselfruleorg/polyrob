"""045 lane 3: a threat-scan hit is counted and attributed, never just swallowed."""
import json

import pytest


@pytest.fixture
def tele_db(tmp_path, monkeypatch):
    p = tmp_path / "telemetry_events.db"
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(p))
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    import core.event_log as el
    el._INSTANCES.clear()
    yield str(p)
    el._INSTANCES.clear()


def _rows(db_path):
    from core.sqlite_util import execute_retry
    return [dict(r) for r in (execute_retry(
        db_path, "SELECT kind, attrs FROM telemetry_events", (), fetch="all") or [])]


def test_flag_records_origin_and_source(tele_db):
    from core.security.threat_report import report_threat
    report_threat("skill", user_id="u1", source="skill_writer", detail="ignore previous")
    rows = _rows(tele_db)
    assert rows[0]["kind"] == "injection_flagged"
    a = json.loads(rows[0]["attrs"])
    assert a["origin"] == "skill"
    assert a["source"] == "skill_writer"


def test_detail_is_truncated_not_stored_whole(tele_db):
    from core.security.threat_report import report_threat
    report_threat("url", detail="x" * 5000)
    a = json.loads(_rows(tele_db)[0]["attrs"])
    assert len(a["detail"]) <= 200


def test_unknown_origin_is_rejected(tele_db):
    from core.security.threat_report import report_threat
    with pytest.raises(ValueError):
        report_threat("nowhere")


def test_recording_is_fail_open(tele_db, monkeypatch):
    from core.security import threat_report

    def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(threat_report, "_log", _boom)
    threat_report.report_threat("url")  # must not raise
