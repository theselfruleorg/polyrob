"""D22/D33/D46/D69 — the delivery section stops being confident and wrong."""
import os
import time

from core.status_snapshot import (
    Section, _delivery_section, _email_tool_drop_note, _outbound_queue_lines,
    _rail_verdict_line, moves_section,
)
from core.surfaces.outbound_queue import OutboundDeliveryQueue


def _tele(rows):
    sec = Section(name="telemetry")
    sec.data["rows"] = rows
    return sec


# --- D22: the queue's dead letters reach a seat -----------------------------

def test_a_missing_outbox_reads_unavailable_and_is_never_created(tmp_path):
    sec = Section(name="delivery")
    _outbound_queue_lines(sec, str(tmp_path))
    assert sec.data["outbound_queue"] is None
    assert any("unavailable" in line for line in sec.lines)
    assert not os.path.exists(os.path.join(str(tmp_path), "outbox.db"))


def test_a_dead_letter_is_named_and_raises_a_health_item(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       os.path.join(str(tmp_path), "telemetry_events.db"))
    q = OutboundDeliveryQueue(os.path.join(str(tmp_path), "outbox.db"))
    q.enqueue(idempotency_key="k", session_key="s", surface_id="telegram",
              dest="777", payload="the report")
    rows = q.claim_due(now=time.time() + 1)
    q.dead_letter(rows[0]["id"], "chat not found")
    sec = Section(name="delivery")
    _outbound_queue_lines(sec, str(tmp_path))
    assert sec.data["outbound_queue"]["dead"] == 1
    assert any("777" in line and "chat not found" in line for line in sec.lines)
    assert [h.key for h in sec.health] == ["outbound_dead_letters"]


def test_the_queue_is_looked_for_beside_the_surface_stores_not_the_telemetry_db(
        tmp_path, monkeypatch):
    """⚠️ Revalidation: the path is ``data_dir``, NOT ``dirname(telemetry db)``.

    ``core/surfaces/bootstrap.py`` builds the queue at
    ``<config.data_dir>/outbox.db``, beside ``surfaces.db`` and
    ``correspondents.db``. ``telemetry_events.db`` resolves on the SIDECAR axis
    (``sidecar_db_path``, plus a ``TELEMETRY_EVENT_LOG_PATH`` override), which
    on a server is one directory ABOVE ``config.data_dir`` — so deriving the
    queue from it rendered a live queue, dead letters and all, as "no outbox.db
    on this deployment". The two tests above both happened to put the two files
    in ONE directory, which is exactly what hid it.
    """
    from core.status_snapshot import _outbound_queue_db

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       str(tmp_path / "telemetry_events.db"))
    assert _outbound_queue_db(str(data_dir)) == str(data_dir / "outbox.db")

    q = OutboundDeliveryQueue(str(data_dir / "outbox.db"))
    q.enqueue(idempotency_key="k", session_key="s", surface_id="slack",
              dest="C1", payload="the report")
    sec = Section(name="delivery")
    _outbound_queue_lines(sec, str(data_dir))
    assert sec.data["outbound_queue"]["pending"] == 1
    assert not any("unavailable" in line for line in sec.lines)


def test_the_registered_queue_wins_over_any_path_derivation(tmp_path):
    """In the process that OWNS the bus, the queue is a live service.

    Asking the container is an answer, not a guess, and it cannot disagree with
    the object the router is actually enqueuing into. The path stays the
    fallback for a seat with no bus (`polyrob doctor`, the CLI).
    """
    live = OutboundDeliveryQueue(str(tmp_path / "elsewhere.db"))
    live.enqueue(idempotency_key="k", session_key="s", surface_id="telegram",
                 dest="7", payload="hi")

    class _Container:
        def get_service(self, name):
            return live if name == "outbound_queue" else None

    sec = Section(name="delivery")
    # `data_dir` holds no outbox.db at all — the path branch would say
    # "unavailable" and be wrong about a queue that is right there.
    _outbound_queue_lines(sec, str(tmp_path / "nothing-here"), container=_Container())
    assert sec.data["outbound_queue"]["pending"] == 1
    assert not any("unavailable" in line for line in sec.lines)


# --- D46: "cap used" is the number the gate counts --------------------------

def test_cap_used_excludes_the_lanes_the_cap_cannot_deny(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       os.path.join(str(tmp_path), "telemetry_events.db"))
    now = time.time()
    rows = [
        {"kind": "user_delivery", "source": "agent_send", "ts": now,
         "attrs": {"outcome": "sent", "lane": "normal"}},
        {"kind": "user_delivery", "source": "credit_sentinel", "ts": now,
         "attrs": {"outcome": "sent", "lane": "critical"}},
        {"kind": "user_delivery", "source": "message_tool", "ts": now,
         "attrs": {"outcome": "sent", "lane": "exempt"}},
    ]
    sec = _delivery_section("u", str(tmp_path), _tele(rows), None, now)
    assert sec.data["consumed"] == 1        # not 3
    assert sec.data["uncounted"] == 2
    assert any("uncapped lane" in line for line in sec.lines)


# --- D69: the SMTP line states a condition, never an unseen effect ----------

def test_the_smtp_line_does_not_assert_a_drop_under_agentmail(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "agentmail")
    assert "no longer request" not in _email_tool_drop_note()
    assert "AgentMail" in _email_tool_drop_note()


def test_the_smtp_line_does_not_assert_a_drop_under_a_frozen_toolset(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("STABLE_AUTONOMOUS_TOOLSET", "true")
    assert "still load" in _email_tool_drop_note()


def test_the_smtp_line_matches_what_effective_autonomous_tools_actually_does(
        monkeypatch, tmp_path):
    """Pinned against the OWNER of the rule.

    `core` may not import `agents.task.constants` (layering ratchet), so the
    condition is re-derived in the snapshot. This test CAN import it, and is
    what stops the two drifting.
    """
    monkeypatch.setenv("VERDICTS_DB_PATH", os.path.join(str(tmp_path), "v.db"))
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.delenv("STABLE_AUTONOMOUS_TOOLSET", raising=False)
    import core.credential_verdicts as cv
    cv._reset_for_tests()
    cv.record_rejection("smtp", "k", code="535")
    from agents.task.constants import effective_autonomous_tools
    dropped = "email" not in effective_autonomous_tools()
    assert dropped is ("no longer request" in _email_tool_drop_note())
    cv._reset_for_tests()


def test_an_imap_verdict_renders_its_own_line():
    from core.credential_verdicts import Verdict
    v = Verdict(kind="imap", key="imap.x:me@x", first_seen=time.time() - 7200,
                last_seen=time.time(), count=4, code="auth",
                remedy="re-check the mailbox password")
    text, severity, key = _rail_verdict_line(v)
    assert "INBOUND" in text and "NOT being received" in text
    assert key == "email_inbound_rejected"


# --- the public moves helper ------------------------------------------------

def test_moves_section_is_public_and_never_confidently_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       os.path.join(str(tmp_path), "telemetry_events.db"))
    from core.event_log import TelemetryEventLog
    log = TelemetryEventLog(os.path.join(str(tmp_path), "telemetry_events.db"))
    log.record("wallet_spend", user_id="u", attrs={
        "action": "swap", "asset": "USDC", "counterparty": "0xabc",
        "chain": "base", "amount_usd": 12.5, "result_ref": "0xdead"})
    # an unparseable row must be COUNTED, never dropped
    from core.sqlite_util import execute_retry
    execute_retry(os.path.join(str(tmp_path), "telemetry_events.db"),
                  "INSERT INTO telemetry_events (ts, kind, user_id, session_id, "
                  "source, attrs) VALUES (?,?,?,?,?,?)",
                  (time.time(), "wallet_spend", "u", "", "", "{not json"))
    sec = moves_section("u", str(tmp_path))
    assert sec.data["total"] == 1
    assert sec.data["unreadable_rows"] == 1
    assert sec.data["total_usd"] == 12.5
    assert any("may be incomplete" in line for line in sec.lines)
    assert [h.key for h in sec.health] == ["moves_unreadable"]


def test_moves_section_says_nothing_moved_only_when_it_could_read(tmp_path, monkeypatch):
    """An ABSENT store raises, so `_guarded` renders `unavailable(<reason>)`.

    "nothing moved" is reserved for a store that was read and held nothing —
    the two are different facts and only one is reassuring.
    """
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       os.path.join(str(tmp_path), "telemetry_events.db"))
    import pytest as _pytest
    with _pytest.raises(FileNotFoundError):
        moves_section("u", str(tmp_path))

    from core.event_log import TelemetryEventLog
    TelemetryEventLog(os.path.join(str(tmp_path), "telemetry_events.db"))
    sec = moves_section("u", str(tmp_path))
    assert sec.data["moves"] == []
    assert sec.data["total_usd"] is None   # never 0.0
    assert "nothing moved" in sec.lines[0]
