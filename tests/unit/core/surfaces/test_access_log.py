"""045 lane 1: every inbound decision is recorded, no body text is kept, and a
raising event log never changes a caller's outcome."""
import json

import pytest

from core.surfaces.envelopes import Identity, InboundMessage, SessionSource


def _inbound(text="hello", user="u_x", surface="telegram", chat="c1",
             chat_type="dm", raw="12345"):
    src = SessionSource(surface_id=surface, chat_id=chat, chat_type=chat_type)
    return InboundMessage(text=text,
                          identity=Identity(user_id=user, source=src, raw_user_id=raw))


class _Decision:
    def __init__(self, kind="task_agent", silent=False, reason=None, tier=None):
        self.kind = kind
        self.silent = silent
        self.session_key = "k"
        self.reason = reason
        self.tier = tier


def _rows(db_path):
    import sqlite3

    from core.sqlite_util import execute_retry
    try:
        return [dict(r) for r in (execute_retry(
            db_path, "SELECT kind, user_id, attrs FROM telemetry_events", (),
            fetch="all") or [])]
    except sqlite3.OperationalError as e:
        # The event-log schema is created lazily on first write (TelemetryEventLog
        # .__init__). When the flag is off, record_route never writes, so the
        # table (and sometimes the file) never comes into being — that IS "no
        # rows recorded", not a test infra error.
        if "no such table" in str(e).lower():
            return []
        raise


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


def test_allowed_route_records_inbound_routed(tele_db):
    from core.surfaces.access_log import record_route
    record_route(_inbound(), _Decision(kind="task_agent"), tier="owner")
    rows = _rows(tele_db)
    assert [r["kind"] for r in rows] == ["inbound_routed"]
    a = json.loads(rows[0]["attrs"])
    assert a["surface"] == "telegram"
    assert a["decision"] == "task_agent"
    assert a["tier"] == "owner"


def test_denied_route_records_access_denied_with_reason(tele_db):
    """I2: the name used to promise something the code could not do —
    `RouteDecision` had no `reason` field at all, so this only ever proved that
    `decision` was the string "denied". The reason now rides the decision."""
    from core.surfaces.access_log import record_route
    record_route(_inbound(), _Decision(kind="denied", silent=True,
                                       reason="no_mention", tier="owner"))
    rows = _rows(tele_db)
    assert [r["kind"] for r in rows] == ["access_denied"]
    a = json.loads(rows[0]["attrs"])
    assert a["decision"] == "denied"
    assert a["reason"] == "no_mention"
    assert a["tier"] == "owner", "tier defaults from the decision, no kwarg needed"


def test_an_explicit_tier_kwarg_still_wins(tele_db):
    from core.surfaces.access_log import record_route
    record_route(_inbound(), _Decision(kind="denied", tier="owner"),
                 tier="correspondent")
    assert json.loads(_rows(tele_db)[0]["attrs"])["tier"] == "correspondent"


def test_body_text_is_never_stored(tele_db):
    from core.surfaces.access_log import record_route
    secret = "my private message body"
    record_route(_inbound(text=secret), _Decision(), tier="owner")
    blob = json.dumps(_rows(tele_db))
    assert secret not in blob
    a = json.loads(_rows(tele_db)[0]["attrs"])
    assert a["body_len"] == len(secret)
    assert len(a["body_sha8"]) == 8


def test_attrs_are_a_closed_allowlist(tele_db):
    from core.surfaces.access_log import ROUTE_ATTRS, record_route
    record_route(_inbound(), _Decision(), tier="owner")
    a = json.loads(_rows(tele_db)[0]["attrs"])
    assert set(a) <= set(ROUTE_ATTRS), f"unexpected attrs: {set(a) - set(ROUTE_ATTRS)}"


def test_recording_is_fail_open(tele_db, monkeypatch):
    from core.surfaces import access_log

    def _boom(*a, **k):
        raise RuntimeError("event log is down")

    monkeypatch.setattr(access_log, "_log", _boom)
    access_log.record_route(_inbound(), _Decision(), tier="owner")  # must not raise


def test_flag_off_writes_nothing(tele_db, monkeypatch):
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "off")
    from core.surfaces.access_log import record_route
    record_route(_inbound(), _Decision(), tier="owner")
    assert _rows(tele_db) == []


def test_pre_route_drop_records_reason(tele_db):
    from core.surfaces.access_log import record_pre_route_drop
    record_pre_route_drop(surface="telegram", chat_id="c9", chat_type="group",
                          sender="777", reason="raw_allowlist", body_len=12)
    rows = _rows(tele_db)
    assert rows[0]["kind"] == "access_denied"
    assert json.loads(rows[0]["attrs"])["reason"] == "raw_allowlist"
