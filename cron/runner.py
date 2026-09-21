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


def resolve_cron_tools(payload: Optional[dict]) -> list:
    """057 WS-A: the job's toolset — ``payload.tools`` (verbatim, the existing
    owner-grant contract) > ``payload.rig`` > ``AUTONOMOUS_RIG_DEFAULT`` >
    :func:`default_cron_tools`. Byte-identical while the env is unset, which is
    the shipped default (``full``)."""
    from core.config_policy.rigs import resolve_rig_tools
    return resolve_rig_tools(payload, default_cron_tools())


def resolve_job_provider(payload: Optional[dict]) -> tuple:
    """(provider_to_use, skip) for a durable job's stored provider pin.

    A stored pin records what the operator preferred WHEN THE JOB WAS CREATED. It
    must not outlive the account: prod's 3-hourly digest froze
    ``provider="zai-coding"`` on 2026-07-19, and when that seat hit its weekly cap
    on 2026-08-17 the job stopped for 37 hours while a second credentialed
    provider sat idle.

    Returns ``(name, False)`` for the provider that can serve — the pin when it is
    alive, else the first live credentialed provider — or ``(None, True)`` when
    nothing can, which is the only case where skipping a paid tick is honest.
    """
    from core.runtime_config import operator_provider_pin, resolve_live_provider
    from core.credit_sentinel import credit_sentinel_active
    pinned = (payload or {}).get("provider")
    # An UNPINNED job (payload={}) must prefer the operator's serving pin
    # (CHAT_PROVIDER > DEFAULT_PROVIDER), exactly as goal dispatch does. Without
    # it the resolver falls to canonical order — on prod that was credit-dead
    # OpenRouter, so every status/exit-monitor tick 402'd first, fell back
    # in-session, and re-tripped the credit sentinel every 6h (2026-08-24..28:
    # 1,323 402 lines, five false credit alerts to the owner).
    preferred = pinned or operator_provider_pin()
    live = resolve_live_provider(preferred)
    if live is not None:
        return (live, False)
    # Nothing resolved live. Skip a PAID tick only when the sentinel is genuinely
    # tripped — an unknown credential picture is not evidence of credit death.
    if credit_sentinel_active(preferred):
        return (None, True)
    return (preferred, False)


def _delivery_ev(job, outcome: str, target: str) -> None:
    """Emit a `cron_delivery` event (fail-open) so the out-of-band delivery's
    outcome — sent / deferred / suppressed / already_told / failed — is in the
    durable ledger, not only the journal line. Tell once (2026-09-21): a skipped
    echo is a decision every seat can show, never a silent no-op."""
    try:
        from core.event_log import get_event_log, event_log_enabled
        if event_log_enabled():
            get_event_log().record(
                "cron_delivery", user_id=getattr(job, "user_id", ""),
                source="cron", job_id=getattr(job, "id", None),
                outcome=outcome, target=target)
    except Exception:
        pass


def _cron_ev(job, outcome: str, reason: Optional[str] = None, **extra) -> None:
    """Emit a cron_run event to the durable event log (fail-open). Makes cron
    lifecycle queryable in the uniform autonomy/governance stream, not just the
    episodes table (telemetry audit 2026-07-04)."""
    try:
        from core.event_log import get_event_log, event_log_enabled
        if event_log_enabled():
            get_event_log().record(
                "cron_run", user_id=getattr(job, "user_id", ""),
                source="cron", job_id=getattr(job, "id", None),
                outcome=outcome, reason=reason, **extra)
    except Exception:
        pass

from agents.task.runtime.run_as_session import (
    create_session_accepts as _create_session_accepts,
    run_task_to_outcome as _run_task_to_outcome,
)
from cron.jobs import CronJob

logger = logging.getLogger(__name__)


def _cron_tick_is_active(result: Any) -> bool:
    """A cron tick counts as 'active' (reset the idle-backoff interval to base)
    when something actually ran or failed. A tick that was merely skipped (lock
    contention, REPL busy) or found nothing due counts as idle. Named + module-level
    so it's directly unit-testable without going through the async ticker loop.
    """
    # 057 WS-C (A3): a tick that YIELDED a goal for a due rail did real work (it
    # moved the board), so it must not read as an idle tick and back the ticker off.
    return (bool(getattr(result, "ran", None))
            or bool(getattr(result, "failed", None))
            or bool(getattr(result, "yielded", None)))


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
            # 031: a full owner pause holds the digest too (it is the owner's own
            # $0 report, so only the `all` scope holds it — a `cron` pause keeps it).
            from core.autonomy_control import allows as _allows
            if not _allows("digest").allowed:
                _cron_ev(job, "skipped", "paused_digest")
                return True
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
        # 031 owner pause: ONE predicate (the legacy halt file/env are facets of
        # it). Checked FIRST, before any of the paid-work gates below — a paid
        # cron tick used to fire straight through a halt (G-35).
        from core.autonomy_control import allows as _allows
        _dec = _allows("cron_run")
        if not _dec.allowed:
            logger.warning("cron job %s: %s — $0 skip, agent not invoked", job.id, _dec.reason)
            _cron_ev(job, "skipped", "paused")
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
            _pinned = (payload or {}).get("provider")
            _live, _skip = resolve_job_provider(payload)
            if _skip:
                logger.info("cron job %s: every credentialed provider is credit-dead — $0 skip",
                            job.id)
                _cron_ev(job, "skipped", "credit_sentinel")
                return True
            if _live and _live != _pinned:
                payload = dict(payload or {})
                payload["provider"] = _live
                if _pinned:
                    # A stored pin is a preference, not a death pact. Prod's digest
                    # job froze provider="zai-coding" into its payload on 2026-07-19;
                    # when that account hit its weekly cap the job stopped for 37
                    # hours even though a second credentialed provider was
                    # configured throughout.
                    logger.info("cron job %s: pinned provider %s is unavailable — routing to %s",
                                job.id, _pinned, _live)
                    # The model belongs to the provider that was pinned; drop it so
                    # the run fills a model that the NEW provider actually serves.
                    payload.pop("model", None)
                    _cron_ev(job, "provider_rerouted", _live)
                # Unpinned: `_live` is simply the operator pin (or its live
                # stand-in) — not a reroute, so no event and no log line.
        except Exception:
            logger.debug("cron job %s: provider liveness check skipped", job.id, exc_info=True)
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
        # 044 T20: a job carrying `payload.group` SERVICES a room — the run
        # session IS the room's bound session (PUBLIC profile + room toolset,
        # stamped by bind_chat_surface), and its task is the room's ledger tail
        # since THIS reader's checkpoint. Resolved BEFORE the `started` event so
        # an empty tail is a genuine $0 tick — no session, no start notice, the
        # same shape the wake change-gate emits.
        from agents.task.goals.group_service import (
            build_service_task, close_room_books, room_binding, service_read_mark,
        )
        _room = room_binding(payload)
        _room_task = _room_read_at = _room_sid = None
        if _room is not None:
            from core.config_policy import AutonomyConfig as _RoomCfg
            _src, _key = _room
            _room_read_at = service_read_mark()
            _room_task, _skip = build_service_task(
                getattr(task_agent, "container", None), payload,
                owner_uid=job.user_id,
                max_replies=_RoomCfg.goal_group_max_replies_per_run())
            if _room_task is None:
                _skip = _skip or "no_change"
                logger.info("cron job %s: room %s:%s — $0 tick, agent not invoked (%s)",
                            job.id, _src.surface_id, _src.chat_id, _skip)
                _cron_ev(job, "skipped", _skip)
                return True
            import uuid as _uuid
            _room_sid = str(_uuid.uuid4())
        _cron_ev(job, "started")
        # 019 P2: owner notice at run START (posture-gated default; the one
        # delivery rail dedups/caps). Fail-open.
        try:
            from agents.task.constants import AutonomyConfig as _StartCfg
            if _StartCfg.autonomy_start_notice():
                from core.self_evolution import push_owner_message
                # priority="low" to match the goal dispatcher's start ping
                # (2026-07-20): a start notice is the least valuable thing on
                # the rail and must not spend the slice reserved for results.
                # This leg was missing it, so the two siblings competed on
                # different terms for the same budget.
                await push_owner_message(
                    getattr(task_agent, "container", None),
                    f"▶ cron run started: {(job.task or '')[:120]} ({job.id[:8]})",
                    priority="low", source="lifecycle")
        except Exception:
            logger.debug("cron start notice failed for %s", job.id, exc_info=True)
        from core.runtime_config import resolve_default_provider
        provider = payload.get("provider") or resolve_default_provider()[0]
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
            "tools": resolve_cron_tools(payload),
            "max_steps": payload.get("max_steps", 20),
            "temperature": 0.0,
            "cron": True,
        }
        if _room is not None:
            # The room's own toolset wins: a room session is PUBLIC and
            # `payload.tools` must not be able to widen it.
            if payload.get("tools"):
                logger.warning("cron job %s: payload.tools ignored — a room service "
                               "run uses the room toolset (%s)", job.id,
                               payload.get("tools"))
            from core.surfaces.room_policy import room_tool_ids
            request["task"] = _room_task
            request["tools"] = room_tool_ids()
            request["session_source"] = _room[0]
            request["chat_session_key"] = _room[1]
            # Bind WITHOUT taking over the room's durable chat<->session row
            # (fix round 1, Critical 1), and pre-generate the session id so the
            # `finally` below can close the books after a cancel (Important 5).
            request["bind_write_row"] = False
            request["session_id"] = _room_sid
        try:
            from core.config_policy import AutonomyConfig

            # Legacy branch (CRON_RUN_LOOP=OFF): create-only, return bool(session_info).
            # Kept exactly as-is — run_task_as_session does NOT apply here.
            if not AutonomyConfig.cron_run_loop():
                _cs_kwargs = {"user_id": job.user_id, "request": request}
                # 043 A17: only pass `creator` when the target actually accepts
                # it — a narrow test fake (no **kwargs, no `creator` param)
                # must not start raising TypeError.
                if _create_session_accepts(task_agent.create_session, "creator"):
                    _cs_kwargs["creator"] = "cron"
                session_info = await task_agent.create_session(**_cs_kwargs)
                return bool(session_info)

            # W3 LIVE-BUG FIX: route through the shared helper so create_session AND
            # run_session are both called. §2: consume the RunOutcome envelope —
            # the done() ledger text, never a re-extracted message-history string.
            run = None
            try:
                run = await _run_task_to_outcome(
                    task_agent, user_id=job.user_id, request=request, autonomous=True,
                    creator="cron", cron_job_id=job.id,
                )
            finally:
                # 044 T20 fix round 1 (Important 5): the scheduler's per-job
                # `wait_for` CANCELS this coroutine, so the old post-run call
                # never ran on a timeout and the next tick re-answered every line
                # the cancelled run had already answered. Presented IS handled.
                # Fix round 2 (N2): prefer the run's REAL session id — the
                # pre-generated one is only honoured if create_session took it.
                if _room is not None:
                    close_room_books(
                        task_agent, payload,
                        session_id=getattr(run, "session_id", None) or _room_sid,
                        up_to_ts=_room_read_at)
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

            # A normal autonomous run must terminate through done().  Step-budget
            # exhaustion and swallowed/late cancellation can still leave useful
            # text behind, but that text is not evidence that the scheduled job
            # completed.  Never advance a money rail as successful in that state.
            if run.done_called is False:
                logger.warning(
                    "cron job %s: run ended without done(); treating as incomplete",
                    job.id,
                )
                try:
                    from modules.memory.episodic import finalize_episode
                    await finalize_episode(
                        session_id=session_id, user_id=job.user_id, kind="cron",
                        task=job.task, outcome="failed",
                        spend_usd=run.spend_usd, steps=run.steps,
                        artifacts=run.artifacts,
                        meta={"source": "cron", "job_id": job.id,
                              "reason": "incomplete_no_done"},
                    )
                except Exception:
                    logger.warning("cron episodic write failed", exc_info=True)
                _cron_ev(job, "failed", "incomplete_no_done",
                         duration_s=round(time.time() - _t0, 3),
                         spend_usd=run.spend_usd, steps=run.steps)
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
                    from cron.delivery import deliver_result_ex, delivery_outcome
                    # D45 (2026-09-21): the typed result — a quiet-hours hold, a
                    # dedup, a cap or a pause is `deferred` (recorded, not lost),
                    # never logged as a send FAILURE.
                    state = await deliver_result_ex(
                        task_agent, job, final,
                        target=deliver, deliver_target=payload.get("deliver_target"),
                        session_id=session_id,
                    )
                    # Observability: make proactive delivery verifiable in the journal.
                    # outcome distinguishes a [SILENT] opt-out (suppressed) from a real
                    # send failure (failed) — they used to both log as ok=False.
                    logger.info("cron job %s out-of-band delivery target=%s outcome=%s",
                                job.id, deliver, delivery_outcome(final, state))
                    _delivery_ev(job, delivery_outcome(final, state), str(deliver))
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
