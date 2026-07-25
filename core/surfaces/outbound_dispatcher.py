"""Drains OutboundDeliveryQueue: per-dest token-bucket pace, send via the surface, backoff
on failure, dead-letter after max_attempts. Deterministic backoff base (jitter added only
in the live run loop, not drain_once, so tests are reproducible)."""
import asyncio
import logging
import random
from typing import Callable, Optional

from core.config_policy import dead_target_registry_enabled
from core.surfaces.dead_targets import classify_dead_error
from core.surfaces.envelopes import OutboundMessage
from core.surfaces.outbound_queue import OutboundDeliveryQueue
from core.rate_limit import TokenBucket

logger = logging.getLogger(__name__)

# TYPE_CHECKING import avoids a circular-import risk; the breaker is pure.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.surfaces.circuit import SurfaceCircuitBreaker
    from core.surfaces.dead_targets import DeadTargetStore


class OutboundDispatcher:
    def __init__(self, queue: OutboundDeliveryQueue,
                 surface_lookup: Callable[[str], Optional[object]], *,
                 max_attempts: int = 6, base_backoff: float = 2.0,
                 rate_per_sec: float = 20.0, burst: int = 20,
                 circuit: Optional["SurfaceCircuitBreaker"] = None,
                 dead_targets: Optional["DeadTargetStore"] = None,
                 event_log: Optional[object] = None) -> None:
        self._q = queue
        self._lookup = surface_lookup
        self._max = max_attempts
        self._base = base_backoff
        self._bucket = TokenBucket(rate_per_sec, burst)
        self._cb = circuit   # SurfaceCircuitBreaker | None
        self._dt = dead_targets   # DeadTargetStore | None
        # Injected telemetry event log (e.g. agents.task.telemetry.event_log's
        # TelemetryEventLog) — None by default so this core-tier module never
        # imports the agents tier itself. Task 4's bootstrap wiring passes a
        # real instance in through its own (already-allowlisted) seam.
        self._event_log = event_log
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def attach_event_log(self, event_log: object) -> None:
        """Attach a telemetry event log post-construction (mirrors
        ``MessageRouter.attach_queue``/``attach_dead_targets``). Bootstrap deliberately
        constructs this dispatcher with ``event_log=None`` (core must never import the
        agents-tier telemetry module), so a caller in a tier that MAY import it — e.g.
        the CLI dispatcher-start seam — wires a real instance in here after
        construction. None by default = no-op, byte-identical legacy."""
        self._event_log = event_log

    def _emit_dead_target_event(self, kind: str, *, surface_id: str,
                                 address: Optional[str], reason: str,
                                 session_key: str) -> None:
        if self._event_log is None:
            return
        try:
            self._event_log.record(
                kind, session_id=session_key or "", source=surface_id,
                attrs={"surface": surface_id, "address": address, "reason": reason},
            )
        except Exception:
            pass

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

            # --- Dead-target registry: skip provably-dead targets; dead-letter, never
            # reschedule (a dead target must not burn attempts/circuit budget forever). ---
            if (self._dt is not None and dead_target_registry_enabled()
                    and self._dt.is_dead(surface_id, row["dest"] or "")):
                self._q.dead_letter(row["id"], "dead_target")
                logger.info("outbound dead-target SKIP surface=%s dest=%s",
                            surface_id, row["dest"])
                self._emit_dead_target_event("dead_target_skipped", surface_id=surface_id,
                                              address=row["dest"], reason="registry",
                                              session_key=row["session_key"])
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
                dt_reason = None
                if self._dt is not None and dead_target_registry_enabled():
                    dt_reason = classify_dead_error(surface_id, err)
                if dt_reason:
                    # Definitive liveness signal: mark dead + dead-letter now. NEVER
                    # reschedule a confirmed-dead target, regardless of attempt count.
                    # Own try/except (mirrors message_router): a store fault here is a
                    # dead-target-store problem, NOT a send problem — the row must still
                    # be dead-lettered and the rest of the claimed batch must drain.
                    try:
                        self._dt.mark(surface_id, row["dest"] or "", dt_reason)
                    except Exception as mark_exc:
                        logger.error("outbound dead-target mark failed surface=%s dest=%s: %s",
                                     surface_id, row["dest"], mark_exc)
                    self._q.dead_letter(row["id"], err)
                    logger.info("outbound dead-target MARK surface=%s dest=%s reason=%s",
                                surface_id, row["dest"], dt_reason)
                    self._emit_dead_target_event("dead_target_marked", surface_id=surface_id,
                                                  address=row["dest"], reason=dt_reason,
                                                  session_key=row["session_key"])
                    continue
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
