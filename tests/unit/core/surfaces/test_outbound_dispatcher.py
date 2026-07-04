import os
import pytest
from core.surfaces.outbound_queue import OutboundDeliveryQueue
from core.surfaces.outbound_dispatcher import OutboundDispatcher
from core.surfaces.envelopes import SendResult


class _Surface:
    def __init__(self, results): self._results = list(results); self.sent = []

    @property
    def surface_id(self): return "wa"

    async def send(self, msg):
        self.sent.append(msg.text)
        r = self._results.pop(0)
        if isinstance(r, Exception): raise r
        return r


@pytest.mark.asyncio
async def test_delivers_and_marks_delivered(tmp_path):
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([SendResult(success=True)])
    d = OutboundDispatcher(q, lambda sid: surf, rate_per_sec=1000, burst=1000)
    n = await d.drain_once(now=100.0)
    assert n == 1 and surf.sent == ["hi"]
    assert q.counts()["delivered"] == 1


@pytest.mark.asyncio
async def test_failure_reschedules_then_dead_letters(tmp_path):
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([SendResult(success=False, error="429")] * 2)
    d = OutboundDispatcher(q, lambda sid: surf, max_attempts=2, base_backoff=1.0,
                           rate_per_sec=1000, burst=1000)
    await d.drain_once(now=0.0)
    assert q.counts()["pending"] == 1          # rescheduled, not delivered
    # jump past backoff; second failure hits max_attempts -> dead
    await d.drain_once(now=10_000.0)
    assert q.counts()["dead"] == 1


@pytest.mark.asyncio
async def test_raising_surface_reschedules_not_crash(tmp_path):
    """A surface that raises must not crash the worker — fail-open."""
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([RuntimeError("network down")])
    d = OutboundDispatcher(q, lambda sid: surf, max_attempts=3, base_backoff=1.0,
                           rate_per_sec=1000, burst=1000)
    n = await d.drain_once(now=0.0)
    assert n == 0
    counts = q.counts()
    assert counts["pending"] == 1
    assert counts["dead"] == 0


@pytest.mark.asyncio
async def test_unknown_surface_reschedules(tmp_path):
    """surface_lookup returning None = reschedule with 'no surface' error."""
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="missing", dest="1", payload="hi")
    d = OutboundDispatcher(q, lambda sid: None, max_attempts=3, base_backoff=1.0,
                           rate_per_sec=1000, burst=1000)
    n = await d.drain_once(now=0.0)
    assert n == 0
    assert q.counts()["pending"] == 1


@pytest.mark.asyncio
async def test_rate_limited_row_reschedules_not_delivered(tmp_path):
    """Token bucket exhausted → row put back to pending with future next_attempt_at."""
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([SendResult(success=True)])
    # burst=0 means no tokens available from the start
    d = OutboundDispatcher(q, lambda sid: surf, rate_per_sec=1.0, burst=0)
    n = await d.drain_once(now=0.0)
    assert n == 0
    assert surf.sent == []          # never sent
    assert q.counts()["pending"] == 1


@pytest.mark.asyncio
async def test_stop_sets_event(tmp_path):
    """stop() sets the internal event so run() exits."""
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    d = OutboundDispatcher(q, lambda sid: None)
    assert not d._stop.is_set()
    await d.stop()
    assert d._stop.is_set()
