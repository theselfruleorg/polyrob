"""Cron runner + ticker (roadmap P5).

Two pieces:

- :class:`CronTicker` — drives ``scheduler.tick()`` on an interval. Unit-tested
  via an injected scheduler; the only background-loop concern is here.
- :func:`make_agent_runner` — the LIVE integration point that turns a ``CronJob``
  into an agent session on the existing task-agent core, with the per-run cap. This
  path needs a live agent run to verify (the scheduler's
  unit tests use a fake runner), so it is opt-in via ``CRON_ENABLED`` and is not
  wired into the default app lifecycle.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional


def _cron_ev(job, outcome: str, reason: Optional[str] = None, **extra) -> None:
    """Emit a cron_run event to the durable event log (fail-open). Makes cron
    lifecycle queryable in the uniform autonomy/governance stream, not just the
    episodes table (telemetry audit 2026-07-04)."""
    try:
        from agents.task.telemetry.event_log import get_event_log, event_log_enabled
        if event_log_enabled():
            get_event_log().record(
                "cron_run", user_id=getattr(job, "user_id", ""),
                source="cron", job_id=getattr(job, "id", None),
                outcome=outcome, reason=reason, **extra)
    except Exception:
        pass

from agents.task.runtime.run_as_session import run_task_as_session as _run_task_as_session
from cron.jobs import CronJob

logger = logging.getLogger(__name__)


def _cron_tick_is_active(result: Any) -> bool:
    """A cron tick counts as 'active' (reset the idle-backoff interval to base)
    when something actually ran or failed. A tick that was merely skipped (lock
    contention, REPL busy) or found nothing due counts as idle. Named + module-level
    so it's directly unit-testable without going through the async ticker loop.
    """
    return bool(getattr(result, "ran", None)) or bool(getattr(result, "failed", None))


class CronTicker:
    """Periodically fire a scheduler tick until stopped."""

    def __init__(self, scheduler: Any, interval_seconds: int = 60):
        self.scheduler = scheduler
        self.interval_seconds = interval_seconds

    async def tick_once(self, now: Optional[datetime] = None):
        return await self.scheduler.tick(now=now)

    async def run_forever(self, stop_event: Optional[asyncio.Event] = None) -> None:
        from core.tickers import IntervalTicker
        from agents.task.constants import (
            ticker_idle_backoff_enabled,
            ticker_idle_backoff_max_multiplier,
        )

        is_active = None
        max_interval = None
        if ticker_idle_backoff_enabled():
            is_active = _cron_tick_is_active
            max_interval = self.interval_seconds * ticker_idle_backoff_max_multiplier()

        await IntervalTicker(
            self.scheduler.tick,
            self.interval_seconds,
            is_active=is_active,
            max_interval_seconds=max_interval,
        ).run_forever(stop_event=stop_event)


def make_agent_runner(task_agent: Any) -> Callable[[CronJob], Awaitable[bool]]:
    """Build a runner that executes a CronJob as an isolated agent session.

    LIVE PATH — reuses ``task_agent.create_session``. A cron run gets cross-session
    memory recall like any other session; recall is tenant-scoped, so this is not
    cross-USER contamination. Returns an async ``runner(job) -> bool`` suitable for
    :class:`cron.scheduler.CronScheduler`. The scheduler already enforces
    ``job.max_duration_seconds`` as a hard cap.
    """
    async def runner(job: CronJob) -> bool:
        session_id = None
        _t0 = time.time()
        payload = dict(job.payload or {})
        if not payload.get("wake_agent", True):
            logger.info("cron job %s: wake_agent=False — $0 tick, agent not invoked", job.id)
            _cron_ev(job, "skipped", "wake_agent_false")
            return True
        _cron_ev(job, "started")
        from core.runtime_config import resolve_runtime_config
        provider = payload.get("provider") or resolve_runtime_config(None, None)[0]
        # Fill the provider's default model from the registry when the job doesn't pin
        # one — an autonomous run has no interactive config, and a None model crashes
        # session setup downstream ('.lower()' on None). Mirrors the goal dispatcher.
        model = payload.get("model")
        if not model:
            try:
                from modules.llm.llm_client_registry import get_default_model
                model = get_default_model(provider)
            except Exception:
                model = None
        request = {
            "task": job.task,
            "provider": provider,
            "model": model,
            "tools": payload.get("tools", ["filesystem", "task"]),
            "max_steps": payload.get("max_steps", 20),
            "temperature": 0.0,
            "cron": True,
        }
        try:
            from agents.task.constants import AutonomyConfig

            # Legacy branch (CRON_RUN_LOOP=OFF): create-only, return bool(session_info).
            # Kept exactly as-is — run_task_as_session does NOT apply here.
            if not AutonomyConfig.cron_run_loop():
                session_info = await task_agent.create_session(user_id=job.user_id, request=request)
                return bool(session_info)

            # W3 LIVE-BUG FIX: route through the shared helper so create_session AND
            # run_session are both called and refusals are normalised to (sid, None).
            session_id, final = await _run_task_as_session(
                task_agent, user_id=job.user_id, request=request, autonomous=True
            )

            # Back-half: cron-specific log messages + early returns.
            if session_id is None:
                logger.error("cron job %s: create_session returned no id", job.id)
                return False
            if final is None:
                # run_task_as_session already collapsed refusal/empty → None; we emit the
                # original log so operators see one clear refusal line (no double-logging:
                # run_task_as_session is intentionally silent on refusals).
                logger.warning("cron job %s: run did not complete (refusal or empty)", job.id)
                try:
                    from modules.memory.episodic import finalize_episode
                    await finalize_episode(
                        session_id=session_id, user_id=job.user_id, kind="cron",
                        task=job.task, outcome="failed",
                        meta={"source": "cron", "job_id": job.id},
                    )
                except Exception:
                    logger.warning("cron episodic write failed", exc_info=True)
                _cron_ev(job, "failed", "refusal_or_empty", duration_s=round(time.time() - _t0, 3))
                return False

            # Episodic write happens BEFORE out-of-band delivery (Task 7): delivery's
            # surfaced-mark is a plain UPDATE keyed on session_id, so the row must
            # already exist or the mark is a silent no-op. Order matters here.
            try:
                from modules.memory.episodic import finalize_episode, collect_provenance
                orchestrator = task_agent.get_orchestrator(session_id)
                prov = await collect_provenance(orchestrator)
                await finalize_episode(
                    session_id=session_id, user_id=job.user_id, kind="cron",
                    task=job.task, outcome="done",
                    summary=str(final)[:2000] if final is not None else None,
                    spend_usd=prov["spend_usd"], steps=prov["steps"],
                    artifacts=prov["artifacts"],
                    meta={"source": "cron", "job_id": job.id},
                )
                _cron_ev(job, "done", duration_s=round(time.time() - _t0, 3),
                         spend_usd=prov.get("spend_usd"), steps=prov.get("steps"))
            except Exception:
                logger.warning("cron episodic write failed", exc_info=True)

            # Out-of-band delivery (gated CRON_DELIVERY_ENABLED, default OFF). Runs
            # inside the scheduler's wait_for budget; fail-open — never fails the job.
            deliver = payload.get("deliver")
            if AutonomyConfig.cron_delivery_enabled() and deliver and final:
                try:
                    from cron.delivery import deliver_result, delivery_outcome
                    ok = await deliver_result(
                        task_agent, job, final,
                        target=deliver, deliver_target=payload.get("deliver_target"),
                        session_id=session_id,
                    )
                    # Observability: make proactive delivery verifiable in the journal.
                    # outcome distinguishes a [SILENT] opt-out (suppressed) from a real
                    # send failure (failed) — they used to both log as ok=False.
                    logger.info("cron job %s out-of-band delivery target=%s outcome=%s",
                                job.id, deliver, delivery_outcome(final, ok))
                except Exception as e:  # belt-and-suspenders; delivery is best-effort
                    logger.error("cron job %s delivery error: %s", job.id, e, exc_info=True)

            return True
        except Exception as e:
            logger.error("cron job %s session failed: %s", job.id, e, exc_info=True)
            _cron_ev(job, "failed", "exception", duration_s=round(time.time() - _t0, 3),
                     error=str(e)[:200])
            if session_id:
                try:
                    from modules.memory.episodic import finalize_episode
                    await finalize_episode(
                        session_id=session_id, user_id=job.user_id, kind="cron",
                        task=job.task, outcome="failed", summary=str(e)[:2000],
                        meta={"source": "cron", "job_id": job.id},
                    )
                except Exception:
                    logger.warning("cron episodic write failed", exc_info=True)
            return False

    return runner


def build_cron_ticker(
    task_agent: Any,
    *,
    data_dir: str = "data",
    interval_seconds: int = 60,
) -> CronTicker:
    """Assemble the full cron stack into a :class:`CronTicker` (B-T1).

    Shares the on-disk job store with the agent-facing ``cronjob`` tool — both use
    ``<data_dir>/cron.db`` — so jobs scheduled by the tool are picked up by the
    ticker. The tick lock (``<data_dir>/cron.tick.lock``) keeps a tick safe under
    ``UVICORN_WORKERS>1``. Opt-in: the FastAPI lifespan only calls this when
    ``CRON_ENABLED`` is set, so prod is unchanged by default.
    """
    from cron.jobs import CronJobStore
    from cron.scheduler import CronScheduler

    store = CronJobStore(os.path.join(data_dir, "cron.db"))
    runner = make_agent_runner(task_agent)
    scheduler = CronScheduler(
        store, runner, lock_path=os.path.join(data_dir, "cron.tick.lock")
    )
    return CronTicker(scheduler, interval_seconds=interval_seconds)
