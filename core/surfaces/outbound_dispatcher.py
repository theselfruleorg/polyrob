"""Drains OutboundDeliveryQueue: per-dest token-bucket pace, send via the surface, backoff
on failure, dead-letter after max_attempts. Deterministic backoff base (jitter added only
in the live run loop, not drain_once, so tests are reproducible)."""
import asyncio
import logging
import random
from typing import Callable, Optional

from core.surfaces.envelopes import OutboundMessage
from core.surfaces.outbound_queue import OutboundDeliveryQueue
from core.surfaces.rate_bucket import TokenBucket

logger = logging.getLogger(__name__)

# TYPE_CHECKING import avoids a circular-import risk; the breaker is pure.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.surfaces.circuit import SurfaceCircuitBreaker


class OutboundDispatcher:
    def __init__(self, queue: OutboundDeliveryQueue,
                 surface_lookup: Callable[[str], Optional[object]], *,
                 max_attempts: int = 6, base_backoff: float = 2.0,
                 rate_per_sec: float = 20.0, burst: int = 20,
                 circuit: Optional["SurfaceCircuitBreaker"] = None) -> None:
        self._q = queue
        self._lookup = surface_lookup
        self._max = max_attempts
        self._base = base_backoff
        self._bucket = TokenBucket(rate_per_sec, burst)
        self._cb = circuit   # SurfaceCircuitBreaker | None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def drain_once(self, now: float) -> int:
        delivered = 0
        for row in self._q.claim_due(now):
            surface_id = row["surface_id"]

            # --- Circuit breaker: skip open surfaces; defer 30 s, attempts unchanged ---
            if self._cb is not None and self._cb.is_open(surface_id):
                self._q.reschedule(row["id"], next_attempt_at=now + 30,
                                   attempts=row["attempts"])
                logger.debug("outbound circuit OPEN: surface=%s deferred 30s", surface_id)
                continue

            key = f"{surface_id}:{row['dest']}"
            allowed, retry_after = self._bucket.take(key, now=now)
            if not allowed:
                self._q.reschedule(row["id"], next_attempt_at=now + retry_after,
                                   attempts=row["attempts"])
                continue
            surface = self._lookup(surface_id)
            ok, err = False, "no surface"
            if surface is not None:
                try:
                    res = await surface.send(OutboundMessage(
                        session_key=row["session_key"], text=row["payload"],
                    ))
                    ok = bool(getattr(res, "success", False))
                    err = getattr(res, "error", None) or ("ok" if ok else "send returned False")
                except Exception as exc:  # fail-open: a raising surface reschedules, never crashes
                    ok, err = False, str(exc)
            if ok:
                if self._cb is not None:
                    self._cb.record_ok(surface_id)
                self._q.mark_delivered(row["id"])
                delivered += 1
            else:
                if self._cb is not None:
                    self._cb.record_fail(surface_id)
                attempts = row["attempts"] + 1
                if attempts >= self._max:
                    self._q.dead_letter(row["id"], err)
                    logger.error("outbound DEAD-LETTER id=%s surface=%s dest=%s err=%s",
                                 row["id"], surface_id, row["dest"], err)
                else:
                    backoff = self._base * (2 ** (attempts - 1))
                    self._q.reschedule(row["id"], next_attempt_at=now + backoff,
                                       attempts=attempts, error=err)
        return delivered

    async def run(self, interval: float = 1.0) -> None:
        import time as _t
        while not self._stop.is_set():
            try:
                now_ts = _t.time()
                if now_ts - getattr(self, "_last_reclaim", 0) > 60:
                    self._q.reclaim_inflight(older_than=now_ts - 120)
                    self._last_reclaim = now_ts
                await self.drain_once(now=now_ts + random.uniform(0, 0.05))
            except Exception as exc:
                logger.error("outbound dispatcher loop error: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except Exception:
                self._task.cancel()
