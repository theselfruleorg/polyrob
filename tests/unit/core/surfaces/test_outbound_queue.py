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


# --- OB2 / OB6 / OB15 / OB22 (2026-10-03 audit) --------------------------------

def test_message_key_is_per_message_and_process_stable(tmp_path):
    """OB2: two identical bodies are two messages; the body digest is sha256,
    not the per-process salted hash()."""
    import hashlib
    from core.surfaces.outbound_queue import message_key
    a, b = message_key("s", "Done."), message_key("s", "Done.")
    assert a != b
    digest = hashlib.sha256(b"Done.").hexdigest()[:16]
    assert a.endswith("#" + digest) and b.endswith("#" + digest)


def test_a_delivered_row_is_not_live(tmp_path):
    """OB2: only pending/inflight rows mean 'still on its way'."""
    q = _q(tmp_path)
    q.enqueue(idempotency_key="k", session_key="s", surface_id="wa", dest="1", payload="x")
    assert q.accepted("k") is True
    row = q.claim_due(now=10_000.0)[0]
    assert q.accepted("k") is True          # inflight
    q.mark_delivered(row["id"])
    assert q.accepted("k") is False


def test_claim_due_only_claims_hosted_surfaces(tmp_path):
    """OB5: a process claims only rows for the surfaces it hosts."""
    q = _q(tmp_path)
    q.enqueue(idempotency_key="a", session_key="s", surface_id="email", dest="x", payload="1")
    q.enqueue(idempotency_key="b", session_key="s", surface_id="telegram", dest="1", payload="2")
    rows = q.claim_due(now=10_000.0, surfaces=["telegram"])
    assert [r["surface_id"] for r in rows] == ["telegram"]
    assert q.claim_due(now=10_000.0, surfaces=[]) == []
    assert q.counts()["pending"] == 1


def test_reclaimed_row_cannot_be_renewed_by_the_old_claimer(tmp_path):
    """OB6: after a reclaim the slow first claimer loses its lease and must not
    send; the young lease of a live claimer is never reclaimed."""
    import time
    q = _q(tmp_path)
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="x")
    row = q.claim_due(now=time.time(), token="A")[0]
    assert q.reclaim_inflight() == 0                 # lease is young
    assert q.renew(row["id"], "A") is True
    assert q.reclaim_inflight(older_than=time.time() + 1) == 1
    assert q.renew(row["id"], "A") is False          # lease voided
    row_b = q.claim_due(now=time.time(), token="B")[0]
    assert q.renew(row_b["id"], "A") is False
    assert q.renew(row_b["id"], "B") is True


def test_prune_removes_only_old_terminal_rows(tmp_path):
    """OB15: delivered/dead rows age out; pending/inflight rows never do."""
    import time
    q = _q(tmp_path)
    for k in ("d", "x", "p", "i"):
        q.enqueue(idempotency_key=k, session_key="s", surface_id="wa", dest="1", payload=k)
    rows = {r["idempotency_key"]: r for r in q.claim_due(now=10_000.0)}
    q.mark_delivered(rows["d"]["id"])
    q.dead_letter(rows["x"]["id"], "gone")
    q.reschedule(rows["p"]["id"], next_attempt_at=0, attempts=0)
    future = time.time() + 30 * 86400
    assert q.prune(now=time.time()) == 0             # all inside retention
    assert q.prune(now=future) == 2
    c = q.counts()
    assert c["delivered"] == 0 and c["dead"] == 0
    assert c["pending"] == 1 and c["inflight"] == 1


def test_unserializable_media_refuses_the_row(tmp_path):
    """OB22: media that cannot ride the queue must not be dropped while the row
    is accepted — the caller falls back to a direct send."""
    import pytest
    q = _q(tmp_path)
    with pytest.raises(ValueError):
        q.enqueue(idempotency_key="m", session_key="s", surface_id="wa", dest="1",
                  payload="x", media=[{"path": object()}])
    assert q.row_state("m") is None
