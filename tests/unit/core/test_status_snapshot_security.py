"""045: the security section is always present, honest when unreadable, and
perimeter volume never crowds out the other sections."""
import os

import pytest


def _seed(tmp_path, monkeypatch, n_inbound=3, n_denied=2):
    db = tmp_path / "telemetry_events.db"
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(db))
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    import core.event_log as el
    el._INSTANCES.clear()
    log = el.get_event_log()
    from core import event_kinds as ek
    for _ in range(n_inbound):
        log.record(ek.INBOUND_ROUTED, user_id="u1", source="perimeter",
                   attrs={"sender": "42", "decision": "task_agent"})
    for _ in range(n_denied):
        log.record(ek.ACCESS_DENIED, user_id="u1", source="perimeter",
                   attrs={"sender": "999", "reason": "raw_allowlist"})
    return str(tmp_path)


def test_security_section_is_present_and_counted(tmp_path, monkeypatch):
    data_dir = _seed(tmp_path, monkeypatch)
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("u1", data_dir=data_dir, task_agent=None,
                                 container=None, include_money=False)
    sec = snap.section("security")
    assert sec.data["inbound"] == 3
    assert sec.data["denied"] == 2
    assert sec.data["top_denial_reasons"][0] == ("raw_allowlist", 2)


def test_security_section_is_in_the_section_order():
    from core.status_snapshot import SECTION_ORDER
    assert "security" in SECTION_ORDER


def test_unreadable_store_renders_unavailable_not_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "absent.db"))
    import core.event_log as el
    el._INSTANCES.clear()
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), task_agent=None,
                                 container=None, include_money=False)
    sec = snap.section("security")
    assert sec.state == "unavailable"
    assert sec.reason
    assert "inbound" not in sec.data


def test_perimeter_rows_are_excluded_from_the_shared_telemetry_fetch(tmp_path, monkeypatch):
    import time
    data_dir = _seed(tmp_path, monkeypatch, n_inbound=50, n_denied=10)
    from core.status_snapshot import _read_telemetry
    tele = _read_telemetry("u1", data_dir, time.time() - 3600)
    kinds = {r["kind"] for r in tele.data["rows"]}
    assert "inbound_routed" not in kinds
    assert "access_denied" not in kinds
