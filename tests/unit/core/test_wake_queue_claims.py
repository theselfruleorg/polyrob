"""098 CO-16: every exit from `claimed` is a CAS on the claimant."""
import time

from core.sqlite_util import execute_retry
from core.wake_queue import WAKE_MACHINE, WAKE_STALE_CLAIM_SEC, WakeQueue


def _q(tmp_path):
    return WakeQueue(str(tmp_path / "wakes.db"))


def _age_claim(q, wid):
    execute_retry(q.db_path, "UPDATE session_wakes SET claimed_at=? WHERE id=?",
                  (time.time() - WAKE_STALE_CLAIM_SEC - 5, wid))


def test_table_well_formed():
    assert WAKE_MACHINE.validate() == []


def test_mark_delivered_by_stale_claimer_is_noop(tmp_path):
    q = _q(tmp_path)
    wid = q.enqueue("s", "u", "wake")
    assert [r.id for r in q.claim_pending("A")] == [wid]
    _age_claim(q, wid)                      # A stalls past the stale window
    assert [r.id for r in q.claim_pending("B")] == [wid]
    assert q.renew(wid, "A") is False       # A re-checks before delivering
    assert q.mark_delivered(wid, "A") is False
    assert q.status_of(wid) == "claimed"
    assert q.mark_delivered(wid, "B") is True
    assert q.status_of(wid) == "delivered"


def test_fail_after_prune_does_not_resurrect(tmp_path):
    q = _q(tmp_path)
    wid = q.enqueue("s", "u", "wake")
    q.claim_pending("A")
    execute_retry(q.db_path, "UPDATE session_wakes SET status='dropped', reason='stale' "
                             "WHERE id=?", (wid,))
    assert q.fail(wid, owner="A") is False
    assert q.release(wid, "A") is False
    assert q.status_of(wid) == "dropped"


def test_two_drainers_one_wake_delivers_once(tmp_path):
    q = _q(tmp_path)
    wid = q.enqueue("s", "u", "wake")
    a, b = q.claim_pending("A"), q.claim_pending("B")
    assert len(a) + len(b) == 1
    assert q.mark_delivered(wid, "A" if a else "B") is True
    assert q.mark_delivered(wid, "B" if a else "A") is False


def test_fail_requeues_then_drops(tmp_path):
    q = _q(tmp_path)
    wid = q.enqueue("s", "u", "wake")
    for _ in range(5):
        assert q.claim_pending("A")
        assert q.fail(wid, owner="A")
    assert q.status_of(wid) == "dropped"
