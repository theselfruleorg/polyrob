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


def default_cron_tools() -> list:
    """Posture-aware default cron toolset (WS-8). Posture 0: ['filesystem','task']
    (byte-identical). Posture>=1: + the compute tools so a scheduled build/self-env
    run isn't tool-starved. Mirrors ``goals.dispatcher.default_goal_tools``; resolved
    at call time so posture 0 is unchanged."""
    try:
        from agents.task.goals.dispatcher import default_goal_tools
        return default_goal_tools()
    except Exception:
        from agents.task.constants import BASE_DEFAULT_TOOLS
        return list(BASE_DEFAULT_TOOLS)


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

from agents.task.runtime.run_as_session import run_task_to_outcome as _run_task_to_outcome
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
        from core.config_policy import (
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


def make_agent_runner(task_agent: Any, *, data_dir: str = "data") -> Callable[[CronJob], Awaitable[bool]]:
    """Build a runner that executes a CronJob as an isolated agent session.

    LIVE PATH — reuses ``task_agent.create_session``. A cron run gets cross-session
    memory recall like any other session; recall is tenant-scoped, so this is not
    cross-USER contamination. Returns an async ``runner(job) -> bool`` suitable for
    :class:`cron.scheduler.CronScheduler`. The scheduler already enforces
    ``job.max_duration_seconds`` as a hard cap.
    """
    async def runner(job: CronJob) -> bool:
        payload = dict(job.payload or {})
        # Owner daily digest: a deterministic $0 tick composed from evidence and
        # pushed via the delivery rail — never invokes the model. Routed here
        # BEFORE the wake/change gates. Fail-open (a compose/deliver error is a
        # $0 no-op, not a job failure) so a persistent hiccup can't spin.
        if payload.get("digest"):
            try:
                from cron.digest import digest_enabled_for, run_digest
                # owner-UX P1 T4: user_id + data_dir are already in scope right
                # here (per-job tenant, this runner's own data_dir), so the
                # digest.enabled pref can tighten/override OWNER_DIGEST_ENABLED
                # per-tenant without any new plumbing.
                if not digest_enabled_for(job.user_id, data_dir):
                    _cron_ev(job, "skipped", "digest_disabled")
                    return True
                ok = await run_digest(task_agent, job)
                _cron_ev(job, "done" if ok else "skipped", "digest")
            except Exception:
                logger.warning("cron job %s: digest tick failed", job.id, exc_info=True)
            return True
        # G-35 owner kill-switch: AutonomyConfig.autonomy_halted() (AUTONOMY_HALT env
        # or a halt-file, togglable without a restart) is honored by goal dispatch
        # (dispatcher.py) but was never referenced here — a paid cron tick kept
        # firing straight through a halt. Checked FIRST, before any of the paid-work
        # gates below. The digest branch above stays exempt: it is $0 by
        # construction and is the owner's OWN report (an owner who halted autonomy
        # still wants their digest, not silence about why nothing else ran).
        from core.config_policy import AutonomyConfig
        if AutonomyConfig.autonomy_halted():
            logger.warning("cron job %s: AUTONOMY_HALT active — $0 skip, agent not invoked", job.id)
            _cron_ev(job, "skipped", "halted")
            return True
        # Task 14 (Phase 3 R5): a watchtower cron job carrying
        # payload.subscription_id $0-skips once its subscription lapses.
        # Decision: suspended/canceled -> skip (subscription_permits_work
        # returns False only for a RESOLVED non-active/grace status — a
        # missing/dangling id is permissive, never gating); active/grace ->
        # run (grace keeps delivering while a renewal is chased — the whole
        # point of a grace period). Gated SUBSCRIPTIONS_ENABLED so a job
        # without the feature on never even queries the subscriptions table
        # (byte-identical to today). Fail-open: a lookup error runs the tick
        # rather than silently starving a paying customer's job.
        sub_id = payload.get("subscription_id")
        if sub_id:
            try:
                from modules.x402 import subscriptions as subs
                if subs.subscriptions_enabled() and not await subs.subscription_permits_work(sub_id):
                    logger.info("cron job %s: subscription %s lapsed — $0 skip, "
                               "agent not invoked", job.id, sub_id)
                    _cron_ev(job, "skipped", "subscription_lapsed")
                    return True
            except Exception:
                logger.warning("cron job %s: subscription gate check failed — "
                              "running tick", job.id, exc_info=True)
        if not payload.get("wake_agent", True):
            logger.info("cron job %s: wake_agent=False — $0 tick, agent not invoked", job.id)
            _cron_ev(job, "skipped", "wake_agent_false")
            return True
        # Wake change-gate: a change-gated review tick whose state fingerprint is
        # unchanged since the last SUCCESSFUL run is a $0 tick — same shape as
        # wake_agent=False. Fail-open (any gate error runs the tick); delivery
        # jobs are never gated. The outcome-tagged baseline is recorded AFTER the
        # run (finally below): a failed run never establishes a skippable
        # baseline, so a persistently-failing job always retries.
        _gate_active = False
        try:
            from cron.wake_gate import gate_applies, should_skip_wake
            _gate_active = gate_applies(job)
            if _gate_active and should_skip_wake(job, data_dir=data_dir):
                logger.info("cron job %s: no observable change — $0 tick, agent not invoked", job.id)
                _cron_ev(job, "skipped", "no_change")
                return True
        except Exception:
            logger.warning("cron job %s: wake gate error — running tick", job.id, exc_info=True)
        # §6.3 provider-credit sentinel: an LLM-invoking tick while credits are
        # dead is a guaranteed paid failure — skip as a $0 tick until the latch
        # auto-releases. Digest/wake_agent=false ticks already returned above.
        try:
            from core.credit_sentinel import credit_sentinel_active
            if credit_sentinel_active():
                logger.info("cron job %s: provider-credit sentinel active — $0 skip", job.id)
                _cron_ev(job, "skipped", "credit_sentinel")
                return True
        except Exception:
            pass
        ok = False
        try:
            ok = await _execute(job, payload)
            return ok
        finally:
            if _gate_active:
                try:
                    from cron.wake_gate import record_wake_outcome
                    record_wake_outcome(job, data_dir=data_dir, ok=bool(ok))
                except Exception:
                    logger.warning("cron job %s: wake gate outcome record failed", job.id,
                                   exc_info=True)

    async def _execute(job: CronJob, payload: dict) -> bool:
        session_id = None
        _t0 = time.time()
        _cron_ev(job, "started")
        # 019 P2: owner notice at run START (posture-gated default; the one
        # delivery rail dedups/caps). Fail-open.
        try:
            from agents.task.constants import AutonomyConfig as _StartCfg
            if _StartCfg.autonomy_start_notice():
                from core.self_evolution import push_owner_message
                await push_owner_message(
                    getattr(task_agent, "container", None),
                    f"▶ cron run started: {(job.task or '')[:120]} ({job.id[:8]})")
        except Exception:
            logger.debug("cron start notice failed for %s", job.id, exc_info=True)
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
            "tools": payload.get("tools", default_cron_tools()),
            "max_steps": payload.get("max_steps", 20),
            "temperature": 0.0,
            "cron": True,
        }
        try:
            from core.config_policy import AutonomyConfig

            # Legacy branch (CRON_RUN_LOOP=OFF): create-only, return bool(session_info).
            # Kept exactly as-is — run_task_as_session does NOT apply here.
            if not AutonomyConfig.cron_run_loop():
                session_info = await task_agent.create_session(user_id=job.user_id, request=request)
                return bool(session_info)

            # W3 LIVE-BUG FIX: route through the shared helper so create_session AND
            # run_session are both called. §2: consume the RunOutcome envelope —
            # the done() ledger text, never a re-extracted message-history string.
            run = await _run_task_to_outcome(
                task_agent, user_id=job.user_id, request=request, autonomous=True
            )
            session_id = run.session_id

            # Back-half: cron-specific log messages + early returns.
            if session_id is None:
                logger.error("cron job %s: create_session returned no id", job.id)
                return False
            if run.refusal:
                # run_task_as_session already collapsed refusal/empty → None; we emit the
                # original log so operators see one clear refusal line (no double-logging:
                # run_task_as_session is intentionally silent on refusals).
                logger.warning("cron job %s: run did not complete (refusal or empty)", job.id)
                # Task 10: the sentinel trip moved to error_recovery.py (the
                # universal LLM-error path) — a credit-death refusal is already
                # tripped upstream, inside the real Agent step loop, before this
                # status string is ever formed. This site now only CHECKS the
                # latch (see credit_sentinel_active() above).
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

            final = run.result_text()

            # Episodic write happens BEFORE out-of-band delivery (Task 7): delivery's
            # surfaced-mark is a plain UPDATE keyed on session_id, so the row must
            # already exist or the mark is a silent no-op. Order matters here.
            try:
                from modules.memory.episodic import finalize_episode
                # Provenance was collected into the envelope while the
                # orchestrator was resident (run_task_to_outcome).
                await finalize_episode(
                    session_id=session_id, user_id=job.user_id, kind="cron",
                    task=job.task, outcome="done",
                    summary=final[:2000] if final else None,
                    spend_usd=run.spend_usd, steps=run.steps,
                    artifacts=run.artifacts,
                    meta={"source": "cron", "job_id": job.id},
                )
                _cron_ev(job, "done", duration_s=round(time.time() - _t0, 3),
                         spend_usd=run.spend_usd, steps=run.steps)
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
    runner = make_agent_runner(task_agent, data_dir=data_dir)
    scheduler = CronScheduler(
        store, runner, lock_path=os.path.join(data_dir, "cron.tick.lock")
    )
    return CronTicker(scheduler, interval_seconds=interval_seconds)
