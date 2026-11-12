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
    # 116 W3.6: the 30-day-old pending row is dead-lettered (not deleted);
    # the inflight row is never touched.
    assert c["delivered"] == 0 and c["dead"] == 1
    assert c["pending"] == 0 and c["inflight"] == 1


def test_unserializable_media_refuses_the_row(tmp_path):
    """OB22: media that cannot ride the queue must not be dropped while the row
    is accepted — the caller falls back to a direct send."""
    import pytest
    q = _q(tmp_path)
    with pytest.raises(ValueError):
        q.enqueue(idempotency_key="m", session_key="s", surface_id="wa", dest="1",
                  payload="x", media=[{"path": object()}])
    assert q.row_state("m") is None


# --- 116 W3.6: an unclaimed pending row ends -------------------------------

def _pending_row(q, key, session_key="s"):
    q.enqueue(idempotency_key=key, session_key=session_key, surface_id="gone",
              dest="1", payload=key)


def _state(q, key):
    return q.row_state(key)


def test_prune_dead_letters_a_pending_row_past_max_age_with_a_reason(tmp_path):
    import time
    from core.surfaces.outbound_queue import MAX_PENDING_AGE_SEC
    q = _q(tmp_path)
    _pending_row(q, "old")
    now = time.time()
    q.prune(now=now + MAX_PENDING_AGE_SEC - 60)
    assert _state(q, "old") == "pending"             # not yet 7 d
    q.prune(now=now + MAX_PENDING_AGE_SEC + 60)
    assert _state(q, "old") == "dead"
    dead = q.dead_letters()
    assert len(dead) == 1
    assert "no host claimed this surface in 7 d" in dead[0]["last_error"]


def test_a_row_held_by_an_owner_pause_is_not_dead_lettered(tmp_path):
    import time
    from core.surfaces.outbound_queue import MAX_PENDING_AGE_SEC
    q = _q(tmp_path)
    _pending_row(q, "held", session_key="direct:telegram:1")
    _pending_row(q, "chat", session_key="sess-1")      # not on the proactive rail
    now = time.time()
    later = now + MAX_PENDING_AGE_SEC + 60
    assert q.expire_pending(later, hold=("paused", now + 1)) == 1
    assert _state(q, "held") == "pending"
    assert _state(q, "chat") == "dead"


def test_a_proactive_row_queued_after_the_pause_began_is_not_exempt(tmp_path):
    import time
    from core.surfaces.outbound_queue import MAX_PENDING_AGE_SEC
    q = _q(tmp_path)
    _pending_row(q, "after", session_key="direct:telegram:1")
    now = time.time()
    assert q.expire_pending(now + MAX_PENDING_AGE_SEC + 60, hold=("paused", now - 60)) == 1
    assert _state(q, "after") == "dead"


def test_an_unreadable_pause_record_keeps_every_proactive_row(tmp_path, monkeypatch):
    import time
    from core.surfaces import outbound_queue as oq
    q = _q(tmp_path)
    _pending_row(q, "held", session_key="direct:telegram:1")

    def _boom():
        raise OSError("pause record unreadable")
    monkeypatch.setattr(oq, "owner_pause_hold", _boom)
    assert q.expire_pending(time.time() + oq.MAX_PENDING_AGE_SEC + 60) == 0
    assert _state(q, "held") == "pending"


def test_pending_stats_names_the_oldest_pending_row_and_7d_delivered(tmp_path):
    import time
    from core.surfaces.outbound_queue import DELIVERED_RETENTION_SEC
    q = _q(tmp_path)
    assert q.pending_stats() == {"oldest_pending_ts": None, "delivered_7d": 0}
    for k in ("a", "b"):
        _pending_row(q, k)
    row = q.claim_due(now=time.time(), limit=1)[0]
    q.mark_delivered(row["id"])
    stats = q.pending_stats()
    assert stats["delivered_7d"] == 1
    assert stats["oldest_pending_ts"] is not None
    assert q.pending_stats(now=time.time() + DELIVERED_RETENTION_SEC + 60)["delivered_7d"] == 0
