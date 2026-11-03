"""098 close-out CO-15: a long send renews its queue lease while it runs.

Before, the lease was renewed only once, right before the send, so a send longer
than ``INFLIGHT_LEASE_SEC`` could be reclaimed mid-flight and sent a second time
by another process. A lost renew means another claimer owns the row: the send
finishes and its token-guarded close loses cleanly.
"""
import asyncio
import logging
import os
import time

import pytest

from core.surfaces.envelopes import SendResult
from core.surfaces.outbound_dispatcher import OutboundDispatcher
from core.surfaces.outbound_queue import OutboundDeliveryQueue


class _SlowSurface:
    def __init__(self, seconds, during=None):
        self.seconds, self.during, self.sent = seconds, during, []

    async def send(self, msg):
        self.sent.append(msg.text)
        if self.during is not None:
            await asyncio.sleep(self.seconds / 2)
            self.during()
            await asyncio.sleep(self.seconds / 2)
        else:
            await asyncio.sleep(self.seconds)
        return SendResult(success=True)


def _queue(tmp_path):
    q = OutboundDeliveryQueue(os.path.join(str(tmp_path), "o.db"))
    q.enqueue(idempotency_key="k", session_key="s", surface_id="tg", dest="1",
              payload="hi")
    return q


@pytest.mark.asyncio
async def test_a_long_send_keeps_renewing_its_lease(tmp_path):
    q = _queue(tmp_path)
    renews = []
    real = q.renew
    q.renew = lambda rid, tok: renews.append(time.time()) or real(rid, tok)
    surface = _SlowSurface(0.35)
    d = OutboundDispatcher(q, lambda sid: surface, lease_renew_sec=0.1)
    assert await d.drain_once(time.time()) == 1
    # one renew right before the send + the heartbeat during it
    assert len(renews) >= 3
    assert q.row_state("k") == "delivered"


@pytest.mark.asyncio
async def test_a_short_send_does_not_wait_for_the_heartbeat(tmp_path):
    q = _queue(tmp_path)
    d = OutboundDispatcher(q, lambda sid: _SlowSurface(0.0), lease_renew_sec=60.0)
    t0 = time.monotonic()
    assert await d.drain_once(time.time()) == 1
    assert time.monotonic() - t0 < 1.0


@pytest.mark.asyncio
async def test_a_lost_renew_is_logged_and_the_close_loses_cleanly(tmp_path, caplog):
    q = _queue(tmp_path)
    other = OutboundDeliveryQueue(q.db_path)

    def steal():
        # Another process reclaims the row and claims it under its own token.
        other.reclaim_inflight(older_than=time.time() + 1)
        assert other.claim_due(time.time(), token="B")

    surface = _SlowSurface(0.4, during=steal)
    d = OutboundDispatcher(q, lambda sid: surface, lease_renew_sec=0.1)
    with caplog.at_level(logging.WARNING, logger="core.surfaces.outbound_dispatcher"):
        await d.drain_once(time.time())
    assert surface.sent == ["hi"]
    assert any("during a long send" in r.getMessage() for r in caplog.records)
    # The other claimer still owns it; this worker's close did not touch it.
    assert q.row_state("k") == "inflight"
    (row_id,) = other._claims
    assert other.mark_delivered(row_id) is True
    assert q.row_state("k") == "delivered"
