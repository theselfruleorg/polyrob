"""Cron tick scheduler (roadmap P5).

Finds due jobs, runs each through an injected async ``runner`` under a hard
duration cap, then reschedules (recurring) or completes (one-shot). A file-based
tick lock makes a tick safe to fire from multiple processes (``UVICORN_WORKERS>1``)
without double-running jobs. The runner is injected so the scheduler is fully
unit-testable without the agent stack; the production runner (see
``cron.runner``) reuses the agent core with the per-run cap.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

try:
    import fcntl
except ImportError:  # pragma: no cover — no flock on this platform: the lock is a no-op
    fcntl = None
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable, List, Optional

from core.cron_outcome import (FAILED_OUTCOMES, FAILURE_STREAK_BY, FAILURE_STREAK_LIMIT,
                               LATE_AFTER_SECONDS, LATE_MONEY_SKIP_SECONDS, span_text)
from core.cron_slots import MODEL_FREE_KEYS
from cron.jobs import CronJob, CronJobStore
from cron.schedule import parse_schedule, ScheduleError

logger = logging.getLogger(__name__)

# The heartbeat bumps the tick lock's mtime every third of this (seconds).
_LOCK_STALE_SECONDS = 600

Runner = Callable[[CronJob], Awaitable[bool]]

#: 2026-10-14 review C-6: the per-run cap a MODEL run gets at least. GLM-5.3
#: cron runs were cut at their 180 s cap at ~40 s per step, half before done().
#: A money-class run (``cron.jobs.is_money_job``: priority=money, or a rig or
#: toolset holding a money tool) reconciles, quotes, gates, swaps and reports,
#: so its floor is higher. Floors only RAISE a cap; the hard ceiling holds.
MODEL_RUN_FLOOR_SECONDS = 300
MONEY_RUN_FLOOR_SECONDS = 900
RUN_CAP_CEILING_SECONDS = 1800


def effective_cap_seconds(job: CronJob) -> float:
    """The cap this run is held to: the job's own cap, raised to the model-run
    floor (money-class: the money floor) and never above the ceiling. A
    model-free job (digest, read or write verb, ``wake_agent=false``) and a
    non-positive cap keep their own value."""
    base = job.max_duration_seconds
    try:
        payload = dict(job.payload or {})
        if base <= 0 or any(payload.get(k) for k in MODEL_FREE_KEYS) \
                or payload.get("wake_agent", True) is False:
            return base
        from cron.jobs import is_money_job
        floor = MONEY_RUN_FLOOR_SECONDS if is_money_job(job) else MODEL_RUN_FLOOR_SECONDS
        return min(max(base, floor), max(base, RUN_CAP_CEILING_SECONDS))
    except Exception:
        return base


def cut_run_may_repeat(job: CronJob) -> bool:
    """107 WS-3: whether a run that a crash cut may run again by itself. A model run can
    load a tool on demand, so its tool list does not prove that it did nothing outward,
    and a repeat can post or spend twice. Only a read with no model turn repeats."""
    payload = job.payload or {}
    if payload.get("write_verb"):
        return False
    return bool(payload.get("digest") or payload.get("read_verb")
                or payload.get("wake_agent") is False)


def _jobs_may_run() -> bool:
    """Whether the owner pause lets a cron run start now. Fail-open (True): only the
    owner line's wording depends on it."""
    try:
        from core.autonomy_control import allows
        return bool(allows("cron_run").allowed)
    except Exception:
        return True


def provider_down(err: object) -> bool:
    """A failure that is the provider's and not the job's, so the failure limit does not
    count it: an outage must not pause every job. *err* is an exception or a status text."""
    try:
        from agents.task.goals.dispatcher import _is_llm_provider_exhausted
        return bool(_is_llm_provider_exhausted(err))
    except Exception:
        logger.debug("outage check failed", exc_info=True)
        return False


def _next_streak(job: CronJob, outcome: str) -> int:
    """The failed runs in a row after this one (``run_note.provider_down`` does not count)."""
    prior = int(((job.payload or {}).get("last_outcome") or {}).get("streak") or 0)
    if outcome == "done":
        return 0
    if outcome not in FAILED_OUTCOMES or (job.run_note or {}).get("provider_down"):
        return prior
    return prior + 1


def _reduces_risk(payload: dict) -> bool:
    """An exit job only sells what the wallet holds, so a late exit beats none."""
    return (isinstance(payload.get("position"), dict)
            or payload.get("mandate_job") in ("exit", "close_all")
            or "exits" in (payload.get("skills") or []))


def _is_late_money_job(job: CronJob, late: float) -> bool:
    """107 D4: a money job too far past its slot does not run. A read with no model turn
    moves nothing, an exit job reduces risk, and a run that waits on an owner answer is
    not late."""
    if late < LATE_MONEY_SKIP_SECONDS:
        return False
    payload = job.payload or {}
    if (payload.get("read_verb") or payload.get("digest") or _reduces_risk(payload)
            or (payload.get("last_outcome") or {}).get("outcome") == "deferred"):
        return False
    from cron.jobs import is_money_job
    return is_money_job(job)


def _seconds_past(now: datetime, slot: Optional[datetime]) -> float:
    """Seconds from *slot* to *now*; 0 with no slot. The store keeps naive LOCAL time,
    but an older writer stored an aware value: it is read as local time, never a crash."""
    if slot is None:
        return 0
    if slot.tzinfo is not None:
        slot = slot.astimezone().replace(tzinfo=None)
    return (now - slot).total_seconds()


def _job_name(job: CronJob) -> str:
    payload = job.payload or {}
    return " ".join(str(payload.get("name") or job.task or "").split())[:60]


class TickLock:
    """Exclusive cross-process lock: a non-blocking ``flock`` on *lock_path*.

    The kernel drops the lock when its holder dies, so there is no stale-steal. The
    old O_EXCL pid file judged a lock stale by its age and stole it by unlink and
    re-create: two workers could both steal it, or a live long tick could lose it.
    The file stays on disk; only the lock on it is held. (``core.state.files.file_lock``
    is the same flock, but it blocks; a tick must skip, not wait.)"""

    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        if self._fd is not None:
            return False  # one TickLock object is one hold
        parent = os.path.dirname(self.lock_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())  # for a person who reads the file
        except OSError:
            pass
        self._fd = fd
        return True

    def refresh(self) -> None:
        """Bump the lock-file mtime, so a reader sees a live tick. The lock itself
        never goes stale while its holder lives."""
        try:
            os.utime(self.lock_path, None)
        except OSError:
            pass

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None


@dataclass
class TickResult:
    ran: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    #: FIX 3: cut off by the duration cap while an owner approval ask THIS run
    #: opened was still open — not the job's failure (see ``_run_one``). 107: also a
    #: money job that gave up a slot because it was too late.
    deferred: List[str] = field(default_factory=list)
    #: 057 WS-C (A3): goal ids pre-empted for a due pre-empting job on this tick.
    #: A tick that yielded DID work (it moved the board), so the idle-backoff
    #: ticker must not treat it as an empty tick.
    yielded: List[str] = field(default_factory=list)
    skipped_locked: bool = False
    skipped_busy: bool = False
    #: 102 T-1: held for memory or disk. Not a failure: due jobs stay due.
    skipped_pressure: bool = False



def release_scope_paused(store, now: datetime) -> List[str]:
    """Resume every job paused BY the pause scope once its run kind may run
    again. Called on the scheduler tick; idempotent (a CAS on status)."""
    from core.autonomy_control import allows
    from core.cron_lanes import PAUSE_SCOPE_BY
    released: List[str] = []
    try:
        rows = [j for j in store.list(user_id=None, enabled_only=True)
                if j.status == "paused"
                and ((j.payload or {}).get("paused") or {}).get("by") == PAUSE_SCOPE_BY]
    except Exception:
        logger.warning("release_scope_paused: store unreadable", exc_info=True)
        return released
    allowed: dict = {}
    for job in rows:
        kind = "digest" if (job.payload or {}).get("digest") else "cron_run"
        if kind not in allowed:
            allowed[kind] = bool(allows(kind).allowed)
        if not allowed[kind]:
            continue
        try:
            nxt = parse_schedule(job.schedule_spec).next_run_after(now)
        except ScheduleError:
            nxt = None
        if store.resume(job.id, user_id=None, next_run_at=nxt or now):
            released.append(job.id)
    if released:
        logger.info("cron: the pause is lifted — resumed %d job(s) created while "
                    "paused: %s", len(released), ", ".join(released))
    return released


class CronScheduler:
    def __init__(self, store: CronJobStore, runner: Runner, *, lock_path: str):
        self.store = store
        self.runner = runner
        self.lock_path = lock_path
        #: 031: the run in progress (so an owner pause can cancel it) + the flag
        #: that tells _run_due the cancellation was a pause, not a failure.
        self._current: Optional[asyncio.Task] = None
        self._current_job: Optional[CronJob] = None
        self._pause_cancelled = False
        #: FIX 3: set by _run_one when a cap timeout is attributable to an OPEN
        #: owner-approval ask this run raised; tells _run_due not to blame the job.
        self._approval_deferred = False
        #: 056 WS5: async hook(job) -> [goal ids] set by the runtime (None = never yield).
        self._yield_hook = None
        #: Routines review 2026-10-29: async hook(job, outcome, reason, at) for a failed run.
        self._failure_hook = None
        #: 107: async hook(text) for an owner line about a job that stopped.
        self._notice_hook = None

    def set_failure_hook(self, hook) -> None:
        """``async hook(job, outcome, reason, at)`` — called after a run that
        FAILED (or ended partial) is recorded; the runtime wires the owner
        notice (``cron.delivery.notify_failed_run``). None = no notice."""
        self._failure_hook = hook

    def set_notice_hook(self, hook) -> None:
        """``async hook(text)`` — one owner line on the critical ``job_stopped`` lane,
        wired by ``cron.runner``. None = no line."""
        self._notice_hook = hook

    async def _tell_owner(self, job: CronJob, text: str, now: datetime) -> None:
        hook = getattr(self, "_notice_hook", None)
        if hook is None:
            return
        try:
            # The time keeps a second stop with the same words from reading as a repeat.
            await hook(f"{text} ({now.strftime('%m-%d %H:%M')})")
        except Exception:
            logger.warning("cron job %s: owner line failed", job.id, exc_info=True)

    def _end_without_run(self, job: CronJob, now: datetime, reason: str,
                         *, outcome: str, last_run_at: Optional[datetime]) -> str:
        """A claimed job gives up this slot: a recurring job goes to its next slot and a
        one-time job ends as failed. Returns the words for the owner line."""
        self.store.record_outcome(job.id, outcome, now, reason=reason,
                                  streak=int(((job.payload or {}).get("last_outcome") or {})
                                             .get("streak") or 0))
        nxt = None
        if not job.one_shot:
            try:
                nxt = parse_schedule(job.schedule_spec).next_run_after(now)
            except ScheduleError:
                nxt = None
        self.store.update_after_run(job.id, last_run_at=last_run_at, next_run_at=nxt,
                                    status="scheduled" if nxt else "failed", expected=job)
        jid = job.id[:8]
        if nxt:
            when = nxt.strftime('%m-%d %H:%M')
            if not _jobs_may_run():
                return f"Its next run is {when}; jobs are paused, so it runs after /resume."
            return f"Its next run is {when}. /job run {jid} runs it now."
        return "It will not run again."

    async def _settle_cut_runs(self, now: datetime) -> None:
        """107 WS-3: a run that a crash or a shutdown cut does not repeat an outward
        effect. One path for both, so the owner hears of it from the next process too."""
        try:
            candidates = self.store.cut_runs()
        except Exception:
            logger.warning("cron: cut runs unreadable", exc_info=True)
            return
        for candidate in candidates:
            try:
                await self._settle_cut_run(candidate, now)
            except Exception:
                logger.warning("cron job %s: cut run not settled", candidate.id, exc_info=True)

    async def _settle_cut_run(self, candidate: CronJob, now: datetime) -> None:
        if cut_run_may_repeat(candidate):
            self.store.clear_cut_run(candidate.id)
            return
        job = self.store.claim_for_run(candidate.id)
        if job is None:
            return
        self.store.clear_cut_run(job.id)
        by = "an owner pause" if (job.payload or {}).get("cut_run") == "pause" else "a restart"
        words = self._end_without_run(job, now, f"cut by {by}", outcome="cut",
                                      last_run_at=now)
        await self._tell_owner(
            job, f"⚠ Job {job.id[:8]} \"{_job_name(job)}\" was cut by {by}. It did not "
                 f"repeat by itself, because it could post or spend twice. Check what it did. "
                 f"{words}", now)

    async def _skip_late_money_job(self, job: CronJob, now: datetime, late: float) -> bool:
        """True = the slot is given up. An owner pause is not lateness: the runner's own
        pause check handles that run."""
        if not _is_late_money_job(job, late):
            return False
        from core.autonomy_control import allows
        if not allows("cron_run").allowed:
            return False
        span = span_text(late)
        self._terminal_ev(job, "skipped", "late")
        words = self._end_without_run(job, now, f"{span} late, so it did not run",
                                      outcome="skipped", last_run_at=job.last_run_at)
        await self._tell_owner(
            job, f"⏭ Job {job.id[:8]} \"{_job_name(job)}\" was {span} late and did not run, "
                 f"because it can move money. {words}", now)
        return True

    async def _stop_failing_job(self, job: CronJob, now: datetime, streak: int) -> bool:
        """107 WS-2: pause a recurring job at the failure limit and say so. True = paused."""
        payload = job.payload or {}
        if job.one_shot or payload.get("digest") or streak < FAILURE_STREAK_LIMIT:
            return False
        reason = (job.run_note or {}).get("reason") or ""
        note = {"by": FAILURE_STREAK_BY, "via": "scheduler", "at": now.isoformat(),
                "reason": str(reason)[:200]}
        try:
            if not self.store.pause(job.id, user_id=None, note=note):
                return False
        except Exception:
            logger.warning("cron job %s: failure limit pause failed", job.id, exc_info=True)
            return False
        logger.warning("cron job %s: paused after %d failed runs", job.id, streak)
        why = f" Last reason: {reason}." if reason else ""
        jid = job.id[:8]
        await self._tell_owner(
            job, f"⏸ Job {jid} \"{_job_name(job)}\" paused after {streak} failed runs in a "
                 f"row.{why} /job show {jid} · /job resume {jid}", now)
        return True

    async def _tell_failure(self, job: CronJob, now: datetime, success: bool) -> None:
        hook = getattr(self, "_failure_hook", None)
        note = job.run_note or {}
        outcome = note.get("outcome") or ("done" if success else "failed")
        if hook is None or outcome not in ("failed", "partial"):
            return
        try:
            await hook(job, outcome, note.get("reason") or "", now)
        except Exception:
            logger.warning("cron job %s: failure notice failed", job.id, exc_info=True)

    def set_yield_hook(self, hook) -> None:
        """056 WS5: ``async hook(job) -> list[goal_id]`` — the goal dispatcher's
        `yield_for_rail`, called when a MONEY job is due while a board goal holds
        the process. Wired by polyrob/app/autonomy_runtime.py; None = never yield."""
        self._yield_hook = hook

    async def _maybe_yield_for_money(self, now: datetime) -> List[str]:
        """The goal ids pre-empted for a due PRE-EMPTING job, or ``[]``.

        057 WS-C (A3/B7): called under the held TickLock (a yield outside it left
        a multi-worker double-pre-emption window), and keyed on
        ``cron.jobs.job_preempts`` — ``payload.preempts``, defaulting to the
        money class — rather than on the priority class alone."""
        hook = getattr(self, "_yield_hook", None)
        if hook is None:
            return []
        try:
            from core.config_policy import AutonomyConfig
            if not AutonomyConfig.goal_yield_for_money_rail():
                return []
            from core.interactive_gate import read_turn_marker
            if read_turn_marker() is not None:
                return []  # a human turn outranks every rail (D2)
            from cron.jobs import job_preempts
            # A job that gives up this slot must not cancel a goal for it.
            due = [j for j in self.store.due(now) if job_preempts(j)
                   and not (j.payload or {}).get("cut_run")
                   and not _is_late_money_job(j, _seconds_past(now, j.next_run_at))]
            if not due:
                return []
            job = due[0]
            held = await hook(job)
            if held:
                logger.warning("cron: pre-empting job %s due — yielded running goal(s) %s",
                               job.id, held)
            return list(held or [])
        except Exception:
            logger.debug("yield-for-money probe failed", exc_info=True)
            return []

    async def tick(self, now: Optional[datetime] = None) -> TickResult:
        from core.interactive_gate import is_interactive_busy
        from core.memory_pressure import current_hold, note_hold
        now = now or datetime.now()
        # 057 WS-C (A3): the busy probe and the yield now run INSIDE the TickLock.
        # They used to run before it, so under workers>1 two processes could both
        # decide to pre-empt the same goal for the same due job.
        lock = TickLock(self.lock_path)
        if not lock.acquire():
            logger.debug("cron tick skipped: lock held")
            return TickResult(skipped_locked=True)
        yielded: List[str] = []
        try:
            from core.lifecycle import draining
            if draining():
                # 077: this process is draining for a restart — start nothing
                # new; due jobs stay due for the restarted process.
                return TickResult(skipped_busy=True)
            # 102 T-1: before the money yield, so a job that cannot start here
            # never cancels a goal for its slot.
            pressure = await asyncio.to_thread(
                current_hold, os.path.dirname(self.store.db_path) or ".")
            if pressure is not None:
                due = self.store.due(now)
                if due:
                    note_hold("cron", pressure, waiting=len(due))
                return TickResult(skipped_pressure=True)
            if is_interactive_busy():
                # A human mid-turn (REPL / owner chat) or a goal run holds the shared
                # workspace. 056 WS5: a due PRE-EMPTING job may pre-empt a GOAL (never
                # a human turn); otherwise defer — jobs stay due for the next idle tick.
                yielded = await self._maybe_yield_for_money(now)
                if not (yielded and not is_interactive_busy()):
                    return TickResult(skipped_busy=True, yielded=yielded)
            # C2: also hold the cross-process workspace lock so a cron run in this
            # process doesn't mutate the shared CWD workspace while a `polyrob` REPL in
            # another process is mid-turn. Non-blocking (timeout=0). Acquire is
            # fail-open (any lock error -> defer the tick); errors from the run
            # itself propagate normally (not masked by the lock handling).
            from core.interactive_gate import workspace_turn_lock
            ws = workspace_turn_lock(timeout=0)
            try:
                ws.__enter__()
            except Exception:
                logger.debug("cron tick skipped: workspace lock unavailable/held")
                return TickResult(skipped_busy=True, yielded=yielded)
            # Heartbeat the tick lock's mtime so a reader sees a long tick is live (the
            # flock itself is never stolen from a living holder).
            hb = asyncio.create_task(self._lock_heartbeat(lock))
            try:
                # 057 WS-C (A3): after a yield THIS tick runs only the jobs that
                # earned the pre-emption. One money yield used to open the gate
                # for the whole due set, so the ops backlog ran on the goal's
                # cancelled slot and the rail still waited behind it.
                result = await self._run_due(now, preempting_only=bool(yielded))
                result.yielded = yielded
                return result
            finally:
                hb.cancel()
                try:
                    ws.__exit__(None, None, None)
                except Exception:
                    pass
        finally:
            lock.release()

    async def _lock_heartbeat(self, lock: "TickLock") -> None:
        interval = max(30.0, _LOCK_STALE_SECONDS / 3.0)
        try:
            while True:
                await asyncio.sleep(interval)
                lock.refresh()
        except asyncio.CancelledError:
            pass

    async def _run_due(self, now: datetime, *, preempting_only: bool = False) -> TickResult:
        result = TickResult()
        tick_started = time.monotonic()
        # Reclaim crash-orphaned 'running' jobs here — we hold the TickLock, so any
        # 'running' row is genuinely stale (a live run always writes a terminal status
        # after itself). This replaces the unsafe reclaim-in-__init__.
        self.store.reclaim_stale_running()
        await self._settle_cut_runs(now)
        # 0011: a job created while `/pause cron` was on is stored paused; once
        # the owner lifts the pause it runs (one CAS per row, idempotent).
        release_scope_paused(self.store, now)
        for candidate in self.store.due(now):
            # Atomic claim: only run if WE flipped it scheduled->running. Guards against
            # ever double-running a job (defense-in-depth alongside the TickLock).
            job = self.store.claim_for_run(candidate.id, now=now,
                                            preempting_only=preempting_only)
            if job is None:
                continue
            at = now + timedelta(seconds=int(time.monotonic() - tick_started))
            late = _seconds_past(at, job.next_run_at)
            if await self._skip_late_money_job(job, at, late):
                result.deferred.append(job.id)
                continue
            job.late_seconds = late if late >= LATE_AFTER_SECONDS else 0
            success = await self._run_one(job)
            if self._pause_cancelled and not success:
                # 031: the owner paused mid-run. Not a failure: the job goes back
                # to 'scheduled' with next_run_at untouched (still due; a paused
                # tick $0-skips it and then reschedules it) and the tick stops
                # here — nothing else starts. A run that completed before the
                # cancel landed (success=True) is recorded normally.
                self.store.set_status(job.id, "scheduled")
                if not cut_run_may_repeat(job):
                    # 107 WS-3: a pause cut is a cut too. Lifted before the next tick,
                    # the run would start again from the top and post or spend twice.
                    self.store.mark_cut_run(job.id, by="pause")
                self._pause_cancelled = False
                break
            self._pause_cancelled = False
            if self._approval_deferred and not success:
                # FIX 3: the run ended while an owner approval THIS run raised
                # was still open. Record the run, do not blame it, and leave the
                # job redeemable — /approve leaves a one-shot grant the next
                # attempt consumes. The tick CONTINUES (unlike a pause): one job
                # waiting on the owner says nothing about the next.
                self._approval_deferred = False
                self._record(job, now, False, deferred=True)
                result.deferred.append(job.id)
                continue
            self._approval_deferred = False
            # Jobs run one at a time, so this run ended later than the tick began. An
            # interval counted from the tick start left a job behind a long one only
            # minutes before its next run. Whole seconds keep a fast run on the tick's time.
            ended = now + timedelta(seconds=int(time.monotonic() - tick_started))
            streak = self._record(job, now, success, ended=ended)
            if not await self._stop_failing_job(job, ended, streak):
                await self._tell_failure(job, ended, success)
            (result.ran if success else result.failed).append(job.id)
        return result

    async def _run_one(self, job: CronJob) -> bool:
        self._pause_cancelled = False
        self._approval_deferred = False
        self._current_job = job
        started = time.time()
        cap = effective_cap_seconds(job)
        self._current = asyncio.create_task(self.runner(job))
        try:
            return bool(await asyncio.wait_for(
                self._current, timeout=max(cap, 0.001),
            ))
        except asyncio.TimeoutError:
            # A timeout is a real failure even if a pause cancel raced in: the job
            # DID exhaust its budget, so record it as failed rather than "held".
            self._pause_cancelled = False
            # ...UNLESS the budget went on an owner decision this run is still
            # waiting for (FIX 3). Narrow on purpose: only an ask THIS run opened
            # and that is still OPEN (see cron/approval_defer.py), so the next
            # attempt — which re-polls the same, now pre-existing ask — fails
            # honestly instead of retrying forever.
            ask_id = self._deferring_owner_ask(job, started)
            if ask_id:
                self._approval_deferred = True
                logger.info(
                    "cron job %s hit its %ss cap waiting on owner approval (ask %s) "
                    "— deferred, not failed", job.id, cap, ask_id)
                self._terminal_ev(job, "deferred", f"owner_ask:{ask_id}",
                                  cap_s=cap,
                                  duration_s=round(time.time() - started, 3))
                return False
            logger.warning("cron job %s timed out after %ss", job.id, cap)
            from agents.task.runtime.stop_reason import StopReason, describe_stop_reason
            job.run_note = {"outcome": "failed",
                            "reason": f"{describe_stop_reason(StopReason.WALL_CLOCK)} ({cap:g}s)"}
            self._terminal_ev(job, "cut_by_cap", f"{cap}s",
                              cap_s=cap, stop_reason=StopReason.WALL_CLOCK.value,
                              duration_s=round(time.time() - started, 3))
            return False
        except asyncio.CancelledError:
            if self._pause_cancelled:
                logger.warning("cron job %s cancelled by owner pause", job.id)
                self._terminal_ev(job, "held", "owner_pause",
                                  duration_s=round(time.time() - started, 3))
                return False
            # 098 review: a tick cancelled for any other reason (shutdown,
            # `/autonomy off`) gives the row back at once — it stayed
            # `running` until its lease ran out (max_duration + slack).
            try:
                self.store.set_status(job.id, "scheduled")
                self.store.mark_cut_run(job.id)
            except Exception:
                logger.debug("cron requeue after cancel failed for %s", job.id, exc_info=True)
            raise
        except Exception as e:  # runner blew up — never crash the tick
            logger.error("cron job %s raised: %s", job.id, e, exc_info=True)
            job.run_note = {"outcome": "failed", "provider_down": provider_down(e),
                            "reason": f"the run raised {type(e).__name__}: {str(e)[:80]}"}
            self._terminal_ev(job, "failed", f"{type(e).__name__}: {str(e)[:120]}",
                              duration_s=round(time.time() - started, 3))
            return False
        finally:
            self._current = None
            self._current_job = None

    def cancel_inflight(self) -> bool:
        """031: cancel the run in progress when the owner pause covers its KIND
        (a digest under a `cron`-scoped pause keeps running). Returns True iff a
        run was cancelled; ``_run_due`` then puts the job back to 'scheduled' —
        a pause is not a failure."""
        t = self._current
        if t is None or t.done():
            return False
        from core.autonomy_control import allows
        job = self._current_job
        kind = "digest" if (job is not None and (job.payload or {}).get("digest")) else "cron_run"
        if allows(kind).allowed:
            return False
        self._pause_cancelled = True
        t.cancel()
        return True

    def _deferring_owner_ask(self, job: CronJob, started: float) -> Optional[str]:
        """FIX 3: the OPEN owner-approval ask this run raised, or None. Fail-open
        (any error answers None = record the timeout as a failure, as before)."""
        try:
            from cron.approval_defer import open_owner_ask_since
            return open_owner_ask_since(
                job.user_id, started, cron_db_path=getattr(self.store, "db_path", None))
        except Exception:
            logger.debug("owner-ask probe failed for job %s", job.id, exc_info=True)
            return None

    def _answered_during_run(self, job: CronJob, started: datetime) -> bool:
        """An approved answer to this job's ask landed after the run started
        (``agents/task/goals/rail_answers.py``). Fail-open (False)."""
        try:
            from agents.task.goals.board import GoalBoard
            from agents.task.goals.rail_answers import answered_during_run
            from core.runtime_paths import goals_db_path
            goals_db = goals_db_path(os.path.dirname(os.path.abspath(self.store.db_path)))
            if not os.path.exists(goals_db):
                return False
            return answered_during_run(GoalBoard(goals_db), job.user_id, job.id,
                                       started.timestamp())
        except Exception:
            logger.debug("answered-during-run probe failed for %s", job.id, exc_info=True)
            return False

    @staticmethod
    def _terminal_ev(job: CronJob, outcome: str, reason: Optional[str] = None,
                     **extra) -> None:
        """056 WS1: a terminal `cron_run` event for the outcomes the RUNNER cannot
        see — the cap timeout, an owner-pause cancel, a runner crash. Before this,
        ~70 of 175 started runs in 48 h (2026-09-17→19) left no terminal event and
        the rails read healthier than they were. Fail-open, lazy import (runner
        imports this module)."""
        try:
            from cron.runner import _cron_ev
            _cron_ev(job, outcome, reason, **extra)
        except Exception:
            logger.debug("terminal cron_run event failed for %s", getattr(job, "id", "?"),
                         exc_info=True)

    def _record(self, job: CronJob, now: datetime, success: bool,
                *, deferred: bool = False, ended: Optional[datetime] = None) -> int:
        """*now* is the tick's start (an owner answer since then re-runs the job);
        *ended* is when this run ended, which the record and the next run count from.
        Returns the job's count of failed runs in a row."""
        started, now = now, ended or now
        # 080 WS-2: the last outcome lives on the job row (a recurring job is
        # back to 'scheduled' after every run, so status alone cannot say).
        # Routines review 2026-10-29: with the reason a run did not finish.
        note = job.run_note or {}
        outcome = ("deferred" if deferred
                   else note.get("outcome") or ("done" if success else "failed"))
        streak = _next_streak(job, outcome)
        try:
            self.store.record_outcome(job.id, outcome, now,
                                      reason=None if deferred else note.get("reason"),
                                      report=None if deferred else note.get("report"),
                                      streak=streak, late_by=job.late_seconds)
        except Exception:
            logger.debug("last-outcome write failed for %s", job.id, exc_info=True)
        if job.one_shot:
            if deferred or note.get("hold"):
                # FIX 3: a one-shot cut off on an unanswered owner prompt must not
                # be marked terminal-'failed' — that throws the work away seconds
                # before the owner answers. Keep it due (next_run_at untouched) so
                # the next tick redeems the decision. The same for a run an owner
                # pause or a pause window held (``run_note.hold``): a reminder is
                # never spent on a pause. Other skips (quiet, no change) stay final.
                self.store.update_after_run(
                    job.id, last_run_at=now, next_run_at=job.next_run_at,
                    status="scheduled", expected=job,
                )
                return streak
            self.store.update_after_run(
                job.id, last_run_at=now, next_run_at=None,
                status="done" if success else "failed", expected=job,
            )
            return streak
        # recurring: reschedule from the schedule spec
        try:
            nxt = parse_schedule(job.schedule_spec).next_run_after(now)
        except ScheduleError:
            nxt = None
        status = "scheduled" if nxt else "done"
        if nxt and self._answered_during_run(job, started):
            # 2026-10-07: the owner answered this job's ask while the run was
            # still going; run again now instead of at the next slot.
            nxt = now
        self.store.update_after_run(job.id, last_run_at=now, next_run_at=nxt,
                                    status=status, expected=job)
        return streak
