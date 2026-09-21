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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, List, Optional

from cron.jobs import CronJob, CronJobStore
from cron.schedule import parse_schedule, ScheduleError

logger = logging.getLogger(__name__)

# A held lock older than this (seconds) is considered stale and stolen — guards
# against a crashed worker leaving the tick permanently locked.
_LOCK_STALE_SECONDS = 600

Runner = Callable[[CronJob], Awaitable[bool]]


class TickLock:
    """Exclusive cross-process lock via O_CREAT|O_EXCL, with stale-steal."""

    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        parent = os.path.dirname(self.lock_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            self._fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._is_stale():
                try:
                    os.unlink(self.lock_path)
                except OSError:
                    return False
                return self.acquire()
            return False
        os.write(self._fd, str(os.getpid()).encode())
        return True

    def _is_stale(self) -> bool:
        try:
            age = time.time() - os.path.getmtime(self.lock_path)
        except OSError:
            return False
        return age > _LOCK_STALE_SECONDS

    def refresh(self) -> None:
        """Bump the lock-file mtime so a long-but-LIVE tick is not seen as stale.

        Without this the mtime is fixed at O_CREAT time; a tick that legitimately
        runs longer than _LOCK_STALE_SECONDS (several due jobs each capped at
        ~600s) would be judged stale and stolen by another worker, whose
        reclaim_stale_running() then resets the live worker's rows and double-runs.
        """
        try:
            os.utime(self.lock_path, None)
        except OSError:
            pass

    def _owner_pid(self) -> Optional[int]:
        try:
            with open(self.lock_path, "rb") as f:
                return int((f.read().strip() or b"0").decode() or "0")
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        # Ownership-checked unlink: if a heartbeat gap let our lock be stolen and
        # another worker recreated it, do NOT delete THEIR lock.
        try:
            if self._owner_pid() == os.getpid():
                os.unlink(self.lock_path)
        except OSError:
            pass


@dataclass
class TickResult:
    ran: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    #: FIX 3: cut off by the duration cap while an owner approval ask THIS run
    #: opened was still open — not the job's failure (see ``_run_one``).
    deferred: List[str] = field(default_factory=list)
    #: 057 WS-C (A3): goal ids pre-empted for a due pre-empting job on this tick.
    #: A tick that yielded DID work (it moved the board), so the idle-backoff
    #: ticker must not treat it as an empty tick.
    yielded: List[str] = field(default_factory=list)
    skipped_locked: bool = False
    skipped_busy: bool = False


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

    def set_yield_hook(self, hook) -> None:
        """056 WS5: ``async hook(job) -> list[goal_id]`` — the goal dispatcher's
        `yield_for_rail`, called when a MONEY job is due while a board goal holds
        the process. Wired by core/autonomy_runtime.py; None = never yield."""
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
            due = [j for j in self.store.due(now) if job_preempts(j)]
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
            if is_interactive_busy():
                # A human mid-turn (REPL / owner chat) or a goal run holds the shared
                # workspace. 056 WS5: a due PRE-EMPTING job may pre-empt a GOAL (never
                # a human turn); otherwise defer — jobs stay due for the next idle tick.
                yielded = await self._maybe_yield_for_money(now)
                if not (yielded and not is_interactive_busy()):
                    return TickResult(skipped_busy=True, yielded=yielded)
            # C2: also hold the cross-process workspace lock so a cron run in this
            # process doesn't mutate the shared CWD workspace while a `rob` REPL in
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
            # Heartbeat the tick lock so a legitimately long tick (>_LOCK_STALE_SECONDS)
            # is never seen as stale + stolen by another worker (which would then
            # reclaim + double-run this tick's in-flight jobs).
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
        from cron.jobs import job_preempts
        # Reclaim crash-orphaned 'running' jobs here — we hold the TickLock, so any
        # 'running' row is genuinely stale (a live run always writes a terminal status
        # after itself). This replaces the unsafe reclaim-in-__init__.
        self.store.reclaim_stale_running()
        for job in self.store.due(now):
            if preempting_only and not job_preempts(job):
                continue  # 057 WS-C (A3): it waits for the next tick
            # Atomic claim: only run if WE flipped it scheduled->running. Guards against
            # ever double-running a job (defense-in-depth alongside the TickLock).
            if not self.store.claim_for_run(job.id):
                continue
            success = await self._run_one(job)
            if self._pause_cancelled and not success:
                # 031: the owner paused mid-run. Not a failure: the job goes back
                # to 'scheduled' with next_run_at untouched (still due; a paused
                # tick $0-skips it and then reschedules it) and the tick stops
                # here — nothing else starts. A run that completed before the
                # cancel landed (success=True) is recorded normally.
                self.store.set_status(job.id, "scheduled")
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
            self._record(job, now, success)
            (result.ran if success else result.failed).append(job.id)
        return result

    async def _run_one(self, job: CronJob) -> bool:
        self._pause_cancelled = False
        self._approval_deferred = False
        self._current_job = job
        started = time.time()
        self._current = asyncio.create_task(self.runner(job))
        try:
            return bool(await asyncio.wait_for(
                self._current, timeout=max(job.max_duration_seconds, 0.001),
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
                    "— deferred, not failed", job.id, job.max_duration_seconds, ask_id)
                self._terminal_ev(job, "deferred", f"owner_ask:{ask_id}",
                                  cap_s=job.max_duration_seconds,
                                  duration_s=round(time.time() - started, 3))
                return False
            logger.warning("cron job %s timed out after %ss", job.id, job.max_duration_seconds)
            self._terminal_ev(job, "cut_by_cap", f"{job.max_duration_seconds}s",
                              cap_s=job.max_duration_seconds,
                              duration_s=round(time.time() - started, 3))
            return False
        except asyncio.CancelledError:
            if self._pause_cancelled:
                logger.warning("cron job %s cancelled by owner pause", job.id)
                self._terminal_ev(job, "held", "owner_pause",
                                  duration_s=round(time.time() - started, 3))
                return False
            raise
        except Exception as e:  # runner blew up — never crash the tick
            logger.error("cron job %s raised: %s", job.id, e, exc_info=True)
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
                *, deferred: bool = False) -> None:
        if job.one_shot:
            if deferred:
                # FIX 3: a one-shot cut off on an unanswered owner prompt must not
                # be marked terminal-'failed' — that throws the work away seconds
                # before the owner answers. Keep it due (next_run_at untouched) so
                # the next tick redeems the decision.
                self.store.update_after_run(
                    job.id, last_run_at=now, next_run_at=job.next_run_at,
                    status="scheduled",
                )
                return
            self.store.update_after_run(
                job.id, last_run_at=now, next_run_at=None,
                status="done" if success else "failed",
            )
            return
        # recurring: reschedule from the schedule spec
        try:
            nxt = parse_schedule(job.schedule_spec).next_run_after(now)
        except ScheduleError:
            nxt = None
        status = "scheduled" if nxt else "done"
        self.store.update_after_run(job.id, last_run_at=now, next_run_at=nxt, status=status)
