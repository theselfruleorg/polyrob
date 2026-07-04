import os
from core.surfaces.outbound_queue import OutboundDeliveryQueue


def test_reclaim_inflight_returns_stuck_rows(tmp_path):
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="x")
    row = q.claim_due(now=100.0)[0]          # now inflight
    assert q.claim_due(now=100.0) == []
    n = q.reclaim_inflight(older_than=10_000.0)  # simulate a long-dead worker
    assert n == 1
    assert len(q.claim_due(now=10_001.0)) == 1   # back to deliverable
