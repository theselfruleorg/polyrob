"""D5/D6/D20/D48/D49/D50 — the delivery rail stops reporting work it did not do.

Each test pins one finding from the 2026-09-21 interface audit. The theme is
the same throughout: an outcome the rail REPORTED as success that the owner
never saw, and a memory that then made the loss permanent.
"""
import json
import os
import time

import pytest

from core.event_log import TelemetryEventLog as EventLog
from core.surfaces.outbound_queue import OutboundDeliveryQueue
from core.surfaces.user_delivery import (
    PRIORITY_EXEMPT, _budgeted, _event_lane, deliver_user_message,
)


# --- D5: an INSERT OR IGNORE no-op is not acceptance ------------------------

def test_a_dead_lettered_key_is_not_accepted(tmp_path):
    q = OutboundDeliveryQueue(os.path.join(str(tmp_path), "outbox.db"))
    row = dict(session_key="s", surface_id="telegram", dest="1", payload="hi")
    assert q.enqueue(idempotency_key="k1", **row) is True
    assert q.accepted("k1") is True
    # the queue gave up on it -> a re-enqueue is a no-op AND not acceptance
    rows = q.claim_due(now=time.time() + 1)
    q.dead_letter(rows[0]["id"], "gone")
    assert q.enqueue(idempotency_key="k1", **row) is False
    assert q.accepted("k1") is False
    assert q.row_state("k1") == "dead"
    assert q.dead_letters()[0]["last_error"] == "gone"


def test_an_unknown_key_is_not_accepted(tmp_path):
    q = OutboundDeliveryQueue(os.path.join(str(tmp_path), "outbox.db"))
    assert q.row_state("nope") is None
    assert q.accepted("nope") is False


# --- D6: queued is not sent -------------------------------------------------

class _EvLog:
    def __init__(self):
        self.events = []

    def record(self, kind, *, user_id="", session_id="", source="", ts=None,
               attrs=None, **kw):
        self.events.append({"kind": kind, "user_id": user_id, "source": source,
                            "ts": ts or time.time(), "attrs": dict(attrs or {})})

    def query(self, *, since_ts=None, kind=None, user_id=None, limit=500):
        return [e for e in self.events
                if (kind is None or e["kind"] == kind)
                and (user_id is None or e["user_id"] == user_id)
                and (since_ts is None or e["ts"] >= since_ts)]

    def outcomes(self):
        return [e["attrs"].get("outcome") for e in self.events
                if e["kind"] == "user_delivery"]


class _QueuingRouter:
    """A router whose surface is not local: it can only QUEUE."""

    def __init__(self):
        self.calls = []

    async def send_message_ex(self, addr, text, surface_id="telegram", **kw):
        self.calls.append((addr, text, surface_id))
        return "queued"

    async def send_message(self, addr, text, surface_id="telegram", **kw):
        return True


class _Container:
    def __init__(self, services):
        self._s = services

    def get_service(self, name):
        return self._s.get(name)


@pytest.mark.asyncio
async def test_a_queued_send_is_recorded_as_fallback_and_reported_as_queued():
    """D6: durable acceptance is a promise, not a delivery.

    Recording it as `sent` made the 24h dedup refuse the retry after the queued
    copy dead-lettered — so neither attempt ever reached the owner and the rail
    reported the loss as a successful duplicate-suppression.

    ⚠️ The ROW and the RETURN answer different questions, so they differ on
    purpose. The row is `fallback` because the budget question — did the rail
    do real work? — has the same answer for a queue hand-off as for a refused
    send. The return is `queued`, because the CALLER's question is different:
    `cron/delivery.py` journals a `fallback` as `failed`, and none of the five
    router surfaces it delivers to is hosted in the agent process, so every
    one of their reports was journalled as a broken rail on a working one.
    """
    ev, router = _EvLog(), _QueuingRouter()
    c = _Container({"message_router": router})
    out = await deliver_user_message(c, "12345", "the report", source="agent_send",
                                     event_log=ev)
    assert out == "queued"
    assert ev.outcomes() == ["fallback"]
    assert ev.events[-1]["attrs"].get("queued") is True
    # …and the SAME body may be retried, because nothing was delivered.
    out2 = await deliver_user_message(c, "12345", "the report", source="agent_send",
                                      event_log=ev)
    assert out2 != "deduped"


# --- D20: the `message` tool's rows cannot spend the owner's cap ------------

def test_the_exempt_lane_is_dropped_from_the_counted_window():
    rows = [{"source": "message_tool", "attrs": {"lane": PRIORITY_EXEMPT}},
            {"source": "agent_send", "attrs": {"lane": "normal"}},
            {"source": "credit_sentinel", "attrs": {"lane": "critical"}}]
    assert _event_lane(rows[0]) == PRIORITY_EXEMPT
    assert [r["source"] for r in _budgeted(rows)] == ["agent_send"]


@pytest.mark.asyncio
async def test_an_exempt_send_neither_spends_nor_is_denied_by_the_cap(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "1")
    ev = _EvLog()

    class _Sink:
        def __init__(self):
            self.sent = []

        async def send_message(self, addr, text, **kw):
            self.sent.append(text)
            return True

    sink = _Sink()
    c = _Container({"telegram_sink": sink})
    for i in range(3):
        out = await deliver_user_message(c, "12345", f"exempt {i}",
                                         source="message_tool",
                                         priority=PRIORITY_EXEMPT, event_log=ev)
        assert out == "sent"
    # the cap still has its one slot for everybody else
    assert await deliver_user_message(c, "12345", "normal one",
                                      source="agent_send", event_log=ev) == "sent"
    assert len(sink.sent) == 4


# --- D48: the window is counted in SQL, not truncated at 1000 rows ----------

def test_count_where_counts_rows_a_1000_row_window_would_miss(tmp_path):
    log = EventLog(os.path.join(str(tmp_path), "telemetry_events.db"))
    now = time.time()
    for i in range(1200):
        log.record("user_delivery", user_id="u", source="agent_send", ts=now,
                   attrs={"outcome": "sent", "lane": "normal",
                          "content_hash": f"h{i}"})
    log.record("user_delivery", user_id="u", source="credit_sentinel", ts=now,
               attrs={"outcome": "sent", "lane": "critical", "content_hash": "c"})
    assert log.count_where(kind="user_delivery", user_id="u",
                           attrs_in={"outcome": ("sent",)}) == 1201
    # …and the critical lane is excluded, NULL-tolerantly
    assert log.count_where(kind="user_delivery", user_id="u",
                           attrs_in={"outcome": ("sent",)},
                           attrs_not_in={"lane": ("critical", "exempt")}) == 1200


def test_count_where_is_null_tolerant_for_a_legacy_row(tmp_path):
    log = EventLog(os.path.join(str(tmp_path), "telemetry_events.db"))
    log.record("user_delivery", user_id="u", source="agent_send",
               attrs={"outcome": "sent"})  # no lane stamp (pre-2026-07 row)
    assert log.count_where(kind="user_delivery", user_id="u",
                           attrs_not_in={"lane": ("critical",)}) == 1


# --- D50: /missed drops a body that was later delivered ---------------------

def test_missed_excludes_a_notice_a_later_sent_row_covers(tmp_path):
    from core.surfaces.missed import missed_notices
    from core.surfaces.user_delivery import content_hash

    path = os.path.join(str(tmp_path), "telemetry_events.db")
    log = EventLog(path)
    now = time.time()
    h_lost, h_later = content_hash("still lost"), content_hash("arrived late")
    log.record("owner_notice", user_id="u", ts=now - 100,
               attrs={"text": "[undelivered; source=agent_send] still lost",
                      "content_hash": h_lost})
    log.record("owner_notice", user_id="u", ts=now - 90,
               attrs={"text": "[undelivered; source=agent_send] arrived late",
                      "content_hash": h_later})
    log.record("user_delivery", user_id="u", ts=now - 10,
               attrs={"outcome": "sent", "content_hash": h_later})

    out = missed_notices("u", str(tmp_path), n=5)
    texts = [e["text"] for e in out]
    assert "still lost" in texts
    assert "arrived late" not in texts


def test_missed_keeps_a_legacy_notice_with_no_hash(tmp_path):
    """Cannot tell => SHOW it. Hiding a message the owner may never have seen
    is the one error this view must not make."""
    from core.surfaces.missed import missed_notices

    log = EventLog(os.path.join(str(tmp_path), "telemetry_events.db"))
    log.record("owner_notice", user_id="u",
               attrs={"text": "[undelivered; source=agent_send] old one"})
    assert [e["text"] for e in missed_notices("u", str(tmp_path))] == ["old one"]
