import os
from core.surfaces.outbound_queue import OutboundDeliveryQueue


def _q(tmp_path): return OutboundDeliveryQueue(os.path.join(tmp_path, "outbox.db"))


def test_enqueue_dedups_on_idempotency_key(tmp_path):
    q = _q(tmp_path)
    assert q.enqueue(idempotency_key="t1#0", session_key="s", surface_id="wa",
                     dest="123", payload="hi") is True
    assert q.enqueue(idempotency_key="t1#0", session_key="s", surface_id="wa",
                     dest="123", payload="hi") is False  # duplicate


def test_claim_due_marks_inflight_and_is_exclusive(tmp_path):
    q = _q(tmp_path)
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="x")
    rows = q.claim_due(now=10_000.0)
    assert len(rows) == 1 and rows[0]["state"] == "inflight"
    assert q.claim_due(now=10_000.0) == []  # already claimed


def test_reschedule_then_redue_and_dead_letter(tmp_path):
    q = _q(tmp_path)
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="x")
    row = q.claim_due(now=100.0)[0]
    q.reschedule(row["id"], next_attempt_at=200.0, attempts=1)
    assert q.claim_due(now=150.0) == []        # not due yet
    again = q.claim_due(now=250.0)
    assert len(again) == 1 and again[0]["attempts"] == 1
    q.dead_letter(again[0]["id"], "boom")
    assert q.counts()["dead"] == 1
