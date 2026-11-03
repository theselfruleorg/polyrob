"""098 CO-15: every exit from ``inflight`` is a CAS on the claimer's lease."""
import os
import time

from core.surfaces.outbound_queue import OUTBOUND, OutboundDeliveryQueue


def _q(tmp_path):
    return OutboundDeliveryQueue(os.path.join(str(tmp_path), "o.db"))


def _one(q, key="k"):
    q.enqueue(idempotency_key=key, session_key="s", surface_id="tg", dest="1", payload="hi")


def _state(q):
    return q.row_state("k")


def test_stale_reschedule_after_redelivery_is_noop(tmp_path):
    a, b = _q(tmp_path), _q(tmp_path)        # two workers on one DB
    _one(a)
    (row,) = a.claim_due(time.time(), token="A")
    a.reclaim_inflight(older_than=time.time() + 1)   # A's lease expires
    (row_b,) = b.claim_due(time.time(), token="B")
    assert b.mark_delivered(row_b["id"])
    # A's slow send fails at last: it must NOT put the delivered row back
    assert a.reschedule(row["id"], next_attempt_at=0, attempts=1, error="x") is False
    assert _state(a) == "delivered"


def test_stale_reschedule_while_other_worker_holds_row_is_noop(tmp_path):
    a, b = _q(tmp_path), _q(tmp_path)
    _one(a)
    (row,) = a.claim_due(time.time(), token="A")
    a.reclaim_inflight(older_than=time.time() + 1)
    b.claim_due(time.time(), token="B")
    assert a.reschedule(row["id"], next_attempt_at=0, attempts=1) is False
    assert a.dead_letter(row["id"], "boom") is False
    assert _state(a) == "inflight"


def test_stale_mark_delivered_after_reclaim_by_other_is_noop(tmp_path):
    a, b = _q(tmp_path), _q(tmp_path)
    _one(a)
    (row,) = a.claim_due(time.time(), token="A")
    a.reclaim_inflight(older_than=time.time() + 1)
    b.claim_due(time.time(), token="B")
    assert a.mark_delivered(row["id"]) is False
    # B still owns it and finishes it
    assert b.reschedule(row["id"], next_attempt_at=0, attempts=1) is True
    assert _state(a) == "pending"


def test_late_success_closes_a_reclaimed_unclaimed_row(tmp_path):
    a = _q(tmp_path)
    _one(a)
    (row,) = a.claim_due(time.time(), token="A")
    a.reclaim_inflight(older_than=time.time() + 1)
    assert a.mark_delivered(row["id"]) is True     # sent: no second copy
    assert _state(a) == "delivered"


def test_set_payload_requires_lease(tmp_path):
    a, b = _q(tmp_path), _q(tmp_path)
    _one(a)
    (row,) = a.claim_due(time.time(), token="A")
    assert a.set_payload(row["id"], "rest") is True
    a.reclaim_inflight(older_than=time.time() + 1)
    b.claim_due(time.time(), token="B")
    assert a.set_payload(row["id"], "stale") is False
    got = a.claim_due  # noqa: F841 (row stays B's)
    import sqlite3
    c = sqlite3.connect(a.db_path)
    assert c.execute("SELECT payload FROM outbound_queue").fetchone()[0] == "rest"
    c.close()


def test_explicit_token_overrides_memory(tmp_path):
    a, other = _q(tmp_path), _q(tmp_path)
    _one(a)
    (row,) = a.claim_due(time.time(), token="A")
    assert other.mark_delivered(row["id"], token="WRONG") is False
    assert other.mark_delivered(row["id"], token="A") is True


def test_legacy_tokenless_claim_still_exits(tmp_path):
    a = _q(tmp_path)
    _one(a)
    (row,) = a.claim_due(time.time())
    assert a.reschedule(row["id"], next_attempt_at=0, attempts=1) is True
    (row,) = a.claim_due(time.time())
    assert a.dead_letter(row["id"], "x") is True
    assert _state(a) == "dead"


def test_machine_well_formed():
    assert OUTBOUND.validate() == []
