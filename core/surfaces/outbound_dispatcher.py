"""Drains OutboundDeliveryQueue: per-dest token-bucket pace, send via the surface, backoff
on failure, dead-letter after max_attempts. Deterministic backoff base (jitter added only
in the live run loop, not drain_once, so tests are reproducible)."""
import asyncio
import logging
import random
import uuid
from typing import Any, Callable, Iterable, Optional

from core.config_policy import dead_target_registry_enabled
from core.surfaces.dead_targets import classify_dead_error
from core.surfaces.envelopes import OutboundMessage
from core.surfaces.outbound_queue import (
    INFLIGHT_LEASE_SEC, PROACTIVE_SESSION_PREFIX, OutboundDeliveryQueue, owner_pause_hold,
)
from core.rate_limit import TokenBucket

logger = logging.getLogger(__name__)

#: How long a row held by the owner's pause waits before it is looked at again.
#: Same idiom as the circuit-breaker defer: attempts are NOT burned, so a hold can
#: never dead-letter a message — it is released on the first tick after `resume`.
_PAUSE_HOLD_SEC = 30.0

#: OB5: a row this process claimed but cannot send (its surface is not hosted
#: here) waits this long and keeps its attempts — "no surface" is a fact about
#: THIS process, not a delivery failure.
_NOT_HOSTED_SEC = 15.0

#: 031: session_key prefix of the PROACTIVE, agent-initiated rail
#: (``MessageRouter.send_message``'s cross-process fallback — the `message` tool
#: and cron delivery). An interactive, session-bound reply (the owner's own chat,
#: enqueued by ``publish()``) carries a real session_key and is NEVER held: R5
#: says owner chat keeps working while paused.
_PROACTIVE_SESSION_PREFIX = PROACTIVE_SESSION_PREFIX

#: 098 CO-15: how often a send in progress renews its lease. Well under
#: ``INFLIGHT_LEASE_SEC``, so a long send (a big document, a slow API) is never
#: reclaimed mid-flight and sent a second time by another process.
LEASE_RENEW_SEC = 60.0

# TYPE_CHECKING import avoids a circular-import risk; the breaker is pure.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.surfaces.circuit import SurfaceCircuitBreaker
    from core.surfaces.dead_targets import DeadTargetStore


def unsent_text(full: Any, remaining: Any) -> str:
    """OB7 / 096 W8: the part of *full* a failed send did NOT deliver.

    ``remaining`` is the progress a surface reports on its error
    (``remaining_text``): ``""`` = every word landed (only an extra failed),
    a string = the text from the first chunk that did not go, ``None`` =
    progress unknown. Returns ``""`` (nothing owed), *remaining*, or the whole
    *full* text when progress is unknown. ONE rule for the outbound queue retry
    and the Telegram command reply's ``/missed`` copy, so neither re-sends a
    chunk the reader already has.
    """
    whole = "" if full is None else str(full)
    if not isinstance(remaining, str):
        return whole
    if not remaining.strip():
        return ""
    return remaining


class OutboundDispatcher:
    def __init__(self, queue: OutboundDeliveryQueue,
                 surface_lookup: Callable[[str], Optional[object]], *,
                 max_attempts: int = 6, base_backoff: float = 2.0,
                 rate_per_sec: float = 20.0, burst: int = 20,
                 circuit: Optional["SurfaceCircuitBreaker"] = None,
                 dead_targets: Optional["DeadTargetStore"] = None,
                 event_log: Optional[object] = None,
                 hosted_surfaces: Optional[Callable[[], Iterable[str]]] = None,
                 lease_renew_sec: float = LEASE_RENEW_SEC) -> None:
        self._q = queue
        self._renew_every = float(lease_renew_sec)
        self._lookup = surface_lookup
        # OB5: the surfaces THIS process can send on. None = claim every
        # surface (legacy / tests); bootstrap passes the router's subscriptions.
        self._hosted = hosted_surfaces
        # OB6: this dispatcher's lease id on the rows it claims.
        self._token = uuid.uuid4().hex
        self._max = max_attempts
        self._base = base_backoff
        self._bucket = TokenBucket(rate_per_sec, burst)
        self._cb = circuit   # SurfaceCircuitBreaker | None
        self._dt = dead_targets   # DeadTargetStore | None
        # Injected telemetry event log (e.g. core.event_log's
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

    def _pause_hold(self):
        """031: ``(reason, since)`` when the owner's pause covers proactive
        outbound, else None — ``core.surfaces.outbound_queue.owner_pause_hold``,
        the ONE rule ``prune`` also reads. Fail-OPEN on a probe error; ``allows``
        itself already fails CLOSED on an unreadable record.
        """
        try:
            return owner_pause_hold()
        except Exception:
            logger.debug("outbound pause probe failed (fail-open)", exc_info=True)
        return None

    @staticmethod
    def _held_by_pause(row, since) -> bool:
        """A row is held when it is on the PROACTIVE rail AND was queued at or
        before the pause began. A row queued after the stop got past the send
        gate on an owner turn (``perform_message_send`` refuses every autonomous
        one), so holding it would delay the owner's own errand for no gain. An
        unknown ``since``/``created_at`` reads conservative: hold."""
        if not str(row.get("session_key") or "").startswith(_PROACTIVE_SESSION_PREFIX):
            return False
        if since is None:
            return True
        try:
            return float(row.get("created_at") or 0) <= float(since)
        except (TypeError, ValueError):
            return True

    async def _hold_lease(self, row) -> None:
        """098 CO-15: renew this claimer's lease every ``_renew_every`` seconds
        while the send runs. A renew is a guarded write on THIS claimer's token;
        a lost renew means another claimer owns the row — say so once and stop.
        The send finishes, and the token-guarded close then loses cleanly."""
        renew = getattr(self._q, "renew", None)
        if not callable(renew):
            return
        while True:
            await asyncio.sleep(self._renew_every)
            try:
                held = renew(row["id"], row.get("claim_token"))
            except Exception:
                logger.warning("outbound: lease renew on row %s failed (store)",
                               row["id"], exc_info=True)
                continue
            if not held:
                logger.warning("outbound.lease_lost row=%s during a long send — another "
                               "claimer owns it; this send finishes, its close loses",
                               row["id"])
                return

    async def _send_holding_lease(self, surface, row, message):
        keeper = asyncio.ensure_future(self._hold_lease(row))
        try:
            return await surface.send(message)
        finally:
            keeper.cancel()
            # 098 review (O-10): wait for the keeper without swallowing a cancel
            # of THIS task — `asyncio.wait` never raises the keeper's result.
            await asyncio.wait({keeper})
            if not keeper.cancelled() and keeper.exception() is not None:
                logger.debug("lease keeper ended with %r", keeper.exception())

    def _resume_partial(self, row, remaining) -> bool:
        """OB7: a surface that reports its progress on a failed send. ``""`` =
        the text landed (only a best-effort extra failed): delivered, True.
        A shorter text = resume from it on the retry, never re-send what went.
        ``None`` (progress unknown) keeps the whole row for the retry."""
        if not isinstance(remaining, str):
            return False
        rest = unsent_text(row.get("payload"), remaining)
        if rest == "":
            return True
        if rest != row.get("payload"):
            setter = getattr(self._q, "set_payload", None)
            if callable(setter):
                try:
                    setter(row["id"], rest)
                    logger.info("outbound: row %s partly sent; the retry resumes "
                                "with %d chars", row["id"], len(rest))
                except Exception:
                    logger.warning("outbound: row %s progress not saved (whole "
                                   "row retried)", row["id"], exc_info=True)
        return False

    async def drain_once(self, now: float) -> int:
        delivered = 0
        # One probe per drain (not per row): the record is a file read.
        hold = self._pause_hold()
        surfaces = None
        if self._hosted is not None:
            try:
                surfaces = list(self._hosted() or [])
            except Exception:
                logger.debug("outbound: hosted-surface probe failed (claim all)",
                             exc_info=True)
                surfaces = None
        for row in self._q.claim_due(now, surfaces=surfaces, token=self._token):
            surface_id = row["surface_id"]

            # --- 031 owner pause: HOLD, never drop. State stays 'pending' and
            # attempts are untouched, so the row survives the pause and is
            # delivered on the first tick after `resume`. ---
            if hold is not None and self._held_by_pause(row, hold[1]):
                self._q.reschedule(row["id"], next_attempt_at=now + _PAUSE_HOLD_SEC,
                                   attempts=row["attempts"])
                logger.info("outbound HELD by owner pause: id=%s surface=%s dest=%s (%s)",
                            row["id"], surface_id, row["dest"], hold[0])
                continue

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
            if surface is None:
                # OB5: not hosted here. Hand it back WITHOUT an attempt or a
                # breaker failure — the process that hosts it will claim it.
                self._q.reschedule(row["id"], next_attempt_at=now + _NOT_HOSTED_SEC,
                                   attempts=row["attempts"], error=row.get("last_error"))
                logger.debug("outbound: surface %s not hosted here; row %s handed back",
                             surface_id, row["id"])
                continue
            # OB6: renew the lease right before the send. A row another process
            # reclaimed while this batch waited must not be sent a second time.
            renew = getattr(self._q, "renew", None)
            if callable(renew) and not renew(row["id"], row.get("claim_token")):
                logger.info("outbound: lease on row %s lost (reclaimed); skipped", row["id"])
                continue
            ok, err = False, "no surface"
            if surface is not None:
                try:
                    # 030 L4: reconstruct media — the queue used to drop it, so
                    # enabling durable delivery silently disabled every photo,
                    # document and invoice card.
                    media = []
                    raw_media = row.get("media") if isinstance(row, dict) else None
                    if raw_media:
                        try:
                            import json
                            parsed = json.loads(raw_media)
                            if isinstance(parsed, list):
                                media = parsed
                        except (TypeError, ValueError):
                            logger.warning("outbound drain: bad media JSON on row %s", row["id"])
                    if media:
                        # OB17: the file must still be the one validated at
                        # enqueue — a symlink swapped in since is refused.
                        from core.surfaces.attachments import drop_swapped
                        media, swapped = drop_swapped(media)
                        if swapped:
                            logger.warning("outbound drain: row %s dropped %d attachment(s) "
                                           "changed since enqueue: %s", row["id"],
                                           len(swapped), swapped)
                    res = await self._send_holding_lease(surface, row, OutboundMessage(
                        session_key=row["session_key"], text=row["payload"],
                        media=media,
                    ))
                    ok = bool(getattr(res, "success", False))
                    err = getattr(res, "error", None) or ("ok" if ok else "send returned False")
                    if not ok:
                        ok = self._resume_partial(row, getattr(res, "remaining_text", None))
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
                    self._q.reclaim_inflight(older_than=now_ts - INFLIGHT_LEASE_SEC)
                    self._last_reclaim = now_ts
                await self.drain_once(now=now_ts + random.uniform(0, 0.05))
            except Exception as exc:
                logger.error("outbound dispatcher loop error: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        """Idempotent while running; after stop() it runs again (098 CO-08)."""
        if self._task is None or self._task.done():
            if self._stop.is_set():
                self._stop = asyncio.Event()
            self._task = asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except Exception:
                task.cancel()
