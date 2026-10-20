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

from core.runtime_paths import cron_db_path
import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional


# 2026-10-14 review C-9: the job toolset resolver lives one tier down
# (tools/cronjob_tools.py) so the `cronjob` tool can NAME a job's tools on its
# result without an upward import; these names stay the runner's API.
from tools.cronjob_tools import default_cron_tools, resolve_cron_tools  # noqa: E402,F401


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
    # (DEFAULT_PROVIDER), exactly as goal dispatch does. Without
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


def _delivery_ev(job, outcome: str, target: str, session_id: Optional[str] = None) -> None:
    """Emit a `cron_delivery` event (fail-open) so the out-of-band delivery's
    outcome — sent / deferred / suppressed / already_told / failed — is in the
    durable ledger, not only the journal line. Tell once (2026-09-21): a skipped
    echo is a decision every seat can show, never a silent no-op.

    2026-10-14 review A-9: the run's ``session_id`` rides on the row (its own
    column) beside ``job_id``, so "did this run deliver?" joins to its cost."""
    try:
        from core.event_log import get_event_log, event_log_enabled
        if event_log_enabled():
            get_event_log().record(
                "cron_delivery", user_id=getattr(job, "user_id", ""),
                session_id=str(session_id or ""),
                source="cron", job_id=getattr(job, "id", None),
                outcome=outcome, target=target)
    except Exception:
        pass


def _cron_ev(job, outcome: str, reason: Optional[str] = None, *,
             session_id: Optional[str] = None, **extra) -> None:
    """Emit a cron_run event to the durable event log (fail-open). Makes cron
    lifecycle queryable in the uniform autonomy/governance stream, not just the
    episodes table (telemetry audit 2026-07-04). A run that started a session
    carries its ``session_id`` in the row's own column (2026-10-14 review D-4)."""
    try:
        from core.event_log import get_event_log, event_log_enabled
        if event_log_enabled():
            get_event_log().record(
                "cron_run", user_id=getattr(job, "user_id", ""),
                session_id=str(session_id or ""),
                source="cron", job_id=getattr(job, "id", None),
                outcome=outcome, reason=reason, **extra)
    except Exception:
        pass


def run_ev_attrs(run: Any, session_id: Optional[str], data_dir: Optional[str]) -> dict:
    """The end-of-run facts a ``cron_run`` row carries (2026-10-14 review D-4/A-9):
    ``useful`` — the run sent, produced or signed something (the ONE predicate,
    ``episodic_digest.did_something``) — and the run's token totals and ledger
    cost from ``usage_records`` (``core.status_economics.run_usage_totals``).
    A total that could not be read is ABSENT from the row, never a zero. Never
    raises."""
    out: dict = {}
    _stop = getattr(run, "stop_reason", None)  # 083 WS-1
    if isinstance(_stop, str) and _stop:
        out["stop_reason"] = _stop
    try:
        from agents.task.agent.core.episodic_digest import did_something, run_record_facts
        out["useful"] = bool(did_something(run_record_facts(run)))
    except Exception:
        logger.debug("cron run useful flag skipped", exc_info=True)
    if session_id:
        try:
            from core.status_economics import run_usage_totals
            totals = run_usage_totals(str(session_id), data_dir)
            if totals is not None:
                out.update(llm_calls=totals["calls"], tokens_in=totals["input_tokens"],
                           tokens_cached=totals["cached_tokens"],
                           tokens_out=totals["output_tokens"],
                           cost_usd=totals["cost_usd"])
        except Exception:
            logger.debug("cron run usage totals skipped", exc_info=True)
    return out

#: The regime seam's row for a cron run (`agents.task.session_class`).
_CRON_REGIME = "cron"

_SKIPPED_RE = __import__("re").compile(r"^\s*(?:OUTCOME:\s*)?SKIP(?:PED)?\b")


def declared_outcome(run: Any) -> tuple:
    """``(outcome, why)`` for a run that called done(): ``blocked`` when it
    declared BLOCKED, ``skipped`` when its record OPENS with SKIPPED, else
    ``done`` (2026-10-13 review C-11 — 6 of 18 buyback cycles that were
    blocked or skipped counted as plain success)."""
    if getattr(run, "blocked", False):
        return "blocked", (getattr(run, "blocked_need", None) or "declared BLOCKED")[:300]
    for text in (getattr(run, "outcome_line", None), getattr(run, "done_text", None)):
        first = str(text or "").strip().splitlines()[:1]
        if first and _SKIPPED_RE.match(first[0]):
            return "skipped", first[0].strip()[:300]
    return "done", ""


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
        # W9 (090 R5, D45): a per-job pause window — e.g. the buyback held from
        # T-1 h to 24 h after the mint window. A $0 skip for every job kind.
        from cron.read_job import active_pause_window
        _window = active_pause_window(payload)
        if _window is not None:
            logger.info("cron job %s: inside its pause window %s — $0 skip", job.id, _window)
            _cron_ev(job, "skipped", "pause_window", window_end=_window[1])
            return True
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
        # Impl handoff E: a deterministic WRITE job — the one allowlisted write
        # verb (agent_nft.agent_nft_collection_reveal), no session, no model turn (cron/write_job.py).
        # Held by the owner pause above and the pause window at the top; OFF
        # unless CRON_WRITE_JOBS_ENABLED; only an owner turn writes one.
        if payload.get("write_verb"):
            from cron.write_job import run_write_job
            ok, why = await run_write_job(task_agent, job)
            _cron_ev(job, "done" if ok else (
                "skipped" if why == "write_jobs_disabled" else "failed"),
                why, verb=str((payload.get("write_verb") or {}).get("verb") or ""))
            return True
        # W9: a deterministic READ job — one allowlisted read verb, no session,
        # no model turn (cron/read_job.py). Held by the same owner pause above.
        if payload.get("read_verb"):
            from cron.read_job import run_read_job
            ok, why = await run_read_job(task_agent, job)
            _cron_ev(job, "done" if ok else "failed", why,
                     verb=str((payload.get("read_verb") or {}).get("verb") or ""))
            return True
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
        # Preflight (cron/preflight.py): a job whose declared tool has a live
        # credential verdict is a $0 tick. Fails OPEN on any read error.
        try:
            from cron.preflight import preflight_skip
            _pf = preflight_skip(job, data_dir=data_dir)
            if _pf is not None:
                logger.info("cron job %s: preflight %s — $0 tick, agent not invoked (%s)",
                            job.id, _pf.reason, _pf.attrs)
                _cron_ev(job, "skipped", _pf.reason, **_pf.attrs)
                if _pf.notice:
                    # ONCE per verdict episode (the preflight claimed it). The
                    # one owner rail: dedup, caps, durable fallback.
                    try:
                        from core.surfaces.user_delivery import deliver_user_message
                        await deliver_user_message(
                            getattr(task_agent, "container", None), job.user_id,
                            _pf.notice, source="cron_preflight")
                    except Exception:
                        logger.warning("cron job %s: preflight owner notice not sent",
                                       job.id, exc_info=True)
                return True
        except Exception:
            logger.warning("cron job %s: preflight error — running tick", job.id, exc_info=True)
        # 2026-10-14 review A-12: a watch job's declared $0 pre-check
        # (`payload.precheck`, cron/read_job.py) — one public read verb, no
        # model. Quiet = a $0 tick; anything else (an alert, a move, an error,
        # no baseline) runs the model with the read attached. Fail-open.
        _precheck = None
        try:
            from cron.read_job import run_precheck
            _precheck = await run_precheck(job, data_dir=data_dir)
            if _precheck is not None and _precheck.skip:
                logger.info("cron job %s: pre-check quiet — $0 tick, agent not invoked (%s)",
                            job.id, _precheck.attrs)
                _cron_ev(job, "skipped", _precheck.reason, **_precheck.attrs)
                return True
        except Exception:
            _precheck = None
            logger.warning("cron job %s: pre-check error — running tick", job.id, exc_info=True)
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
            ok = await _execute(job, payload, precheck=_precheck)
            return ok
        finally:
            if _precheck is not None and ok:
                # The value this model run saw is the next tick's baseline.
                from cron.read_job import record_baseline
                record_baseline(job, _precheck.value, data_dir=data_dir)
            if _gate_active:
                try:
                    from cron.wake_gate import record_wake_outcome
                    record_wake_outcome(job, data_dir=data_dir, ok=bool(ok))
                except Exception:
                    logger.warning("cron job %s: wake gate outcome record failed", job.id,
                                   exc_info=True)

    async def _record(job: CronJob, session_id, outcome: str, *, run=None, reason: str = "",
                      error: str = "", summary=None) -> None:
        """The job's run record for one exit path (2026-10-13 review R3)."""
        from agents.task.agent.core.episodic_digest import record_run
        await record_run(regime=_CRON_REGIME, kind="cron", session_id=session_id,
                         user_id=job.user_id, rail_id=f"cron:{job.id}", task=job.task,
                         outcome=outcome, reason=reason, error=error, run=run,
                         summary=summary, meta={"source": "cron", "job_id": job.id})

    async def _record_cut_run(job: CronJob, session_id, *, idle_outcome: str = "cancelled",
                              reason: str = "cut by an owner pause before done()",
                              error: str = "", cap_s: Optional[float] = None) -> None:
        """B5: a run cancelled by its time cap or an owner pause — or one whose
        run call RAISED — never returns a RunOutcome. Read what the resident
        session did and record it — PARTIAL when it sent, produced or signed
        anything, else ``idle_outcome``. ``cap_s`` set = the time cap cut it:
        the record names the wall-clock stop reason. Bounded; never raises."""
        try:
            from core.autonomy_marker import cron_job_for_session
            if not session_id or cron_job_for_session(session_id) != job.id:
                return  # the pre-generated id was not taken: no session to read
            run = None
            try:
                from agents.task.runtime.run_outcome import build_run_outcome
                run = await asyncio.wait_for(
                    build_run_outcome(task_agent, session_id, None), timeout=15)
            except Exception:
                logger.debug("cron %s: cut run unreadable", job.id, exc_info=True)
            if cap_s is not None:
                from agents.task.runtime.stop_reason import StopReason, describe_stop_reason
                if run is not None:
                    run.stop_reason = StopReason.WALL_CLOCK.value  # the cap, not the loop
                reason = (f"{describe_stop_reason(StopReason.WALL_CLOCK)} "
                          f"({cap_s:g}s) before done()")
            from agents.task.agent.core.episodic_digest import (
                did_something, run_record_facts)
            outcome = "partial" if did_something(run_record_facts(run)) else idle_outcome
            await asyncio.wait_for(_record(job, session_id, outcome, run=run,
                                           reason=reason, error=error), timeout=10)
        except Exception:
            logger.warning("cron %s: cut-run record failed", job.id, exc_info=True)

    async def _execute(job: CronJob, payload: dict, *, precheck=None) -> bool:
        session_id = None
        run = None
        _run_sid = None
        _t0 = time.time()
        # 044 T20: a job carrying `payload.group` SERVICES a room — the run
        # session IS the room's bound session (PUBLIC profile + room toolset,
        # stamped by bind_chat_surface), and its task is the room's ledger tail
        # since THIS reader's checkpoint. Resolved BEFORE the `started` event so
        # an empty tail is a genuine $0 tick — no session, no start notice, the
        # same shape the wake change-gate emits.
        from agents.task.goals.group_service import (
            build_service_task, close_room_books, room_binding, room_run_max_steps,
            service_read_mark,
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
        _cron_ev(job, "started", session_id=_room_sid)
        # 019 P2: owner notice at run START (posture-gated default; the one
        # delivery rail dedups/caps). Fail-open.
        try:
            from core.config_policy import AutonomyConfig as _StartCfg
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
            # The per-regime resolver (REGIME_POLICY model_tier) — the same
            # call the goal run makes; money-class jobs get HEAVY_MODEL.
            from agents.task.session_class import CRON as _CRON_REG, regime_model_for
            model = regime_model_for(_CRON_REG, payload, provider)
        if not model:
            try:
                from modules.llm.llm_client_registry import get_default_model
                model = get_default_model(provider)
            except Exception:
                model = None
        # 061 WS-3: the owner's answer to what THIS job asked last time rides
        # into this run (`agents/task/goals/rail_answers.py`). Fail-open.
        # 2026-10-13 review B4: READ here, CONSUMED only after a run that
        # completed — a refused, failed or cut run used to lose "DECLINED, do
        # not re-ask" and the job asked again.
        _task_text = job.task
        _answers, _answer_board, _answer_ids = "", None, []
        try:
            from agents.task.goals.board import GoalBoard
            from agents.task.goals.rail_answers import peek_rail_answers
            from core.runtime_paths import goals_db_path
            _answer_board = GoalBoard(goals_db_path(data_dir))
            _answers, _answer_ids = peek_rail_answers(
                _answer_board, job.user_id, f"cron:{job.id}")
            if _answers:
                _task_text = f"{_answers}\n\n{job.task}"
        except Exception:
            logger.debug("cron %s: rail answers skipped (fail-open)", job.id, exc_info=True)

        def _consume_answers() -> None:
            if not _answer_ids or _answer_board is None:
                return
            try:
                from agents.task.goals.rail_answers import mark_rail_answers_consumed
                mark_rail_answers_consumed(_answer_board, _answer_ids,
                                           run_id=f"cron_run:{job.id}:{int(time.time())}")
            except Exception:
                logger.debug("cron %s: rail answers not marked consumed", job.id, exc_info=True)

        # 2026-10-13 review B6: the job's OWN last runs (outcome, why, what it
        # sent, what it did, tx) — the run record — ride in as <previous-runs>,
        # between the owner's decisions and the job text.
        _rail = f"cron:{job.id}"
        try:
            from agents.task.agent.core.episodic_digest import render_previous_runs
            _prev = await render_previous_runs(user_id=job.user_id, rail_id=_rail,
                                               regime=_CRON_REGIME)
            if _prev:
                _task_text = "\n\n".join(p for p in (_answers, _prev, job.task) if p)
        except Exception:
            logger.debug("cron %s: previous runs skipped (fail-open)", job.id, exc_info=True)
        # 2026-10-06: owner rules written AFTER the job's text override it
        # (the 10-05 PROMO run posted against "approve posts with me").
        from cron.owner_rules_overlay import overlay_for_job
        _overlay = overlay_for_job(job, data_dir)
        if _overlay:
            _task_text = f"{_overlay}\n\n{_task_text}"
        # A-12: what the $0 pre-check already read rides in, so the run does
        # not pay to read the same figure again.
        from cron.read_job import context_block
        _pc_block = context_block(precheck)
        if _pc_block:
            _task_text = f"{_pc_block}\n\n{_task_text}"
        # 081 WS-5.4: a pinned skill that is gone is named at the TOP of the run.
        from core.cron_lanes import missing_skills_notice
        _missing = missing_skills_notice(payload, user_id=job.user_id)
        if _missing:
            _task_text = f"{_missing}\n\n{_task_text}"
        from core.config_policy import AutonomyConfig as _CapCfg
        request = {
            "task": _task_text,
            "provider": provider,
            "model": model,
            "tools": resolve_cron_tools(payload),
            # 083 WS-4: the operator's cap; a stored payload.max_steps is inert —
            # except a room-service job, whose short reply turn keeps its 8
            # (`group_service.room_run_max_steps`).
            "max_steps": (room_run_max_steps() if _room is not None
                          else _CapCfg.autonomous_max_steps()),
            "temperature": 0.0,
            "cron": True,
            # B1: recall searches the job's OWN text — never the overlay or the
            # rail answers prepended to `task` above.
            "recall_seed": job.task,
        }
        # 025: a job's memory scope is cron:<job_id>, regime payload.memory_regime >
        # AUTONOMY_MEMORY_REGIME. {} while scopes are OFF. Cron never promotes: it has
        # no completion judge (the owner promotes via `polyrob owner memory scopes`).
        from modules.memory.scope import cron_label, request_fields
        request.update(request_fields(cron_label(job.id), payload))
        # 060 WS-5: a job pins its doctrine (`payload.skills`) — the daily-X run
        # used to keyword-match ['self-deploy','polyrob-user-guide'] while the
        # posting skills it needed stayed unloaded. Absent = byte-identical.
        from core.config_policy.rigs import pinned_skills
        _pinned_skills = pinned_skills(payload)
        if _pinned_skills:
            request["skills"] = _pinned_skills
        # 068 G2: a declared buy target rides the run as DATA. A malformed one
        # stops the run — silently dropping it would turn a restricted money
        # job into an unrestricted one.
        from core.wallet.buy_target import PAYLOAD_KEY, normalize_target
        try:
            _target = normalize_target(payload.get(PAYLOAD_KEY))
        except ValueError as exc:
            logger.error("cron job %s: invalid %s — run refused: %s", job.id, PAYLOAD_KEY, exc)
            _cron_ev(job, "skipped", "invalid_target_token")
            return False
        if _target:
            # W0: a target the owner typed is trust for this run.
            from core.wallet.buy_target import with_authorship
            request["money_target"] = with_authorship(_target, payload)
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
                if session_info:
                    _consume_answers()   # legacy: creation IS the hand-off
                return bool(session_info)

            # W3 LIVE-BUG FIX: route through the shared helper so create_session AND
            # run_session are both called. §2: consume the RunOutcome envelope —
            # the done() ledger text, never a re-extracted message-history string.
            run = None
            try:
                # CHAT-5: a job's text is the standing authority its run
                # carries (e.g. moderating the room it names).
                from core.autonomy_marker import note_job
                note_job(job.id, job.task, post_spacing_hours=(
                    (job.payload or {}).get("post_spacing_hours")))
                # 2026-10-13 review B5: pre-generate the run's session id so a
                # run CANCELLED by its time cap or an owner pause can still
                # write its run record (it never returns a RunOutcome).
                if "session_id" not in request:
                    import uuid as _uuid_rec
                    request["session_id"] = str(_uuid_rec.uuid4())
                _run_sid = request["session_id"]
                run = await _run_task_to_outcome(
                    task_agent, user_id=job.user_id, request=request, autonomous=True,
                    creator="cron", cron_job_id=job.id,
                )
            except asyncio.CancelledError:
                # The scheduler's wait_for cancels at the time cap; an owner
                # pause cancels too. Elapsed vs the same cap tells them apart.
                from cron.scheduler import effective_cap_seconds
                _cap = effective_cap_seconds(job)
                await _record_cut_run(job, _run_sid, cap_s=(
                    _cap if 0 < _cap <= time.time() - _t0 + 1 else None))
                raise
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
                await _record(job, session_id, "failed", run=run,
                              reason="the run was refused or produced nothing",
                              error=str(run.status or ""))
                _cron_ev(job, "failed", "refusal_or_empty", session_id=session_id,
                         duration_s=round(time.time() - _t0, 3),
                         **run_ev_attrs(run, session_id, data_dir))
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
                # C-03: a run that bought and THEN ran out of steps did real
                # work; it is PARTIAL with a summary of what happened, never a
                # bare "failed" the next run cannot learn from.
                from agents.task.agent.core.episodic_digest import (
                    did_something, run_record_facts)
                _partial = did_something(run_record_facts(run))
                from agents.task.runtime.stop_reason import label_last_report
                _stop = run.stop_description
                _out = "partial" if _partial else "failed"
                # 083 WS-2: the agent's last report at the step limit (its words).
                _last = label_last_report(getattr(run, "exhausted_summary", None), 1500)
                await _record(job, session_id, _out, run=run,
                              reason=f"stopped before done() — {_stop}",
                              summary=(f"{_out.upper()}: stopped before done() — {_stop}"
                                       f" | {_last}") if _last else None)
                _cron_ev(job, "partial" if _partial else "failed", "incomplete_no_done",
                         session_id=session_id, duration_s=round(time.time() - _t0, 3),
                         spend_usd=run.spend_usd, steps=run.steps,
                         **run_ev_attrs(run, session_id, data_dir))
                return False

            final = run.result_text()

            # Episodic write happens BEFORE out-of-band delivery (Task 7): delivery's
            # surfaced-mark is a plain UPDATE keyed on session_id, so the row must
            # already exist or the mark is a silent no-op. Order matters here.
            # The run COMPLETED: the owner decisions it read are now spent (B4).
            _consume_answers()
            # C-11: a run that closed by declaring BLOCKED or SKIPPED did not
            # do the job; its record and its event say so instead of "done".
            _declared, _why = declared_outcome(run)
            try:
                # Provenance was collected into the envelope while the
                # orchestrator was resident (run_task_to_outcome).
                await _record(job, session_id, _declared, run=run, reason=_why,
                              summary=final[:2000] if final else None)
                _cron_ev(job, _declared, _why or None, session_id=session_id,
                         duration_s=round(time.time() - _t0, 3),
                         spend_usd=run.spend_usd, steps=run.steps,
                         **run_ev_attrs(run, session_id, data_dir))
            except Exception:
                logger.warning("cron episodic write failed", exc_info=True)

            # The same declared BLOCKED need on N runs in a row raises ONE owner
            # ask (cron/remedy_streak.py). Fail-open; never changes the outcome.
            try:
                from cron.remedy_streak import observe_and_notify
                _need = run.blocked_need if getattr(run, "blocked", False) else None
                await observe_and_notify(job, _need, data_dir=data_dir,
                                         container=getattr(task_agent, "container", None))
            except Exception:
                logger.debug("cron %s: remedy streak skipped", job.id, exc_info=True)

            # Out-of-band delivery (gated CRON_DELIVERY_ENABLED, default OFF). Runs
            # inside the scheduler's wait_for budget; fail-open — never fails the job.
            # Owner rule (2026-09-29): the report body is what the run SAID with
            # send_message; done() text is the run's record, delivered only when
            # the owner's verbosity asks for records (owner-bound sinks only —
            # cron/delivery.py decides that). A run that sent nothing delivers
            # nothing, as an empty result always did. A [SILENT] record still
            # opts the run out.
            deliver = payload.get("deliver")
            # B8: a PUBLIC sink never gets the run's closing "Done — cost …"
            # line; an owner sink gets the last send_message as before.
            from cron.delivery import report_body
            body = report_body(run, deliver, payload.get("deliver_target"))
            from cron.delivery import SILENT_MARKER, is_silent
            if is_silent(run.done_text):
                body = SILENT_MARKER
            if AutonomyConfig.cron_delivery_enabled() and deliver and body:
                try:
                    from cron.delivery import deliver_result_ex, delivery_outcome
                    from core.prefs import done_records_visible
                    # D45 (2026-09-21): the typed result — a quiet-hours hold, a
                    # dedup, a cap or a pause is `deferred` (recorded, not lost),
                    # never logged as a send FAILURE.
                    state = await deliver_result_ex(
                        task_agent, job, body,
                        target=deliver, deliver_target=payload.get("deliver_target"),
                        session_id=session_id,
                        record=(run.done_text
                                if done_records_visible(job.user_id, data_dir) else None),
                    )
                    # Observability: make proactive delivery verifiable in the journal.
                    # outcome distinguishes a [SILENT] opt-out (suppressed) from a real
                    # send failure (failed) — they used to both log as ok=False.
                    logger.info("cron job %s out-of-band delivery target=%s outcome=%s",
                                job.id, deliver, delivery_outcome(body, state))
                    _delivery_ev(job, delivery_outcome(body, state), str(deliver),
                                 session_id=session_id)
                except Exception as e:  # belt-and-suspenders; delivery is best-effort
                    logger.error("cron job %s delivery error: %s", job.id, e, exc_info=True)

            return True
        except Exception as e:
            logger.error("cron job %s session failed: %s", job.id, e, exc_info=True)
            _cron_ev(job, "failed", "exception", session_id=session_id or _run_sid,
                     duration_s=round(time.time() - _t0, 3), error=str(e)[:200])
            if session_id:
                await _record(job, session_id, "failed", run=run,
                              reason="the run raised", error=f"{type(e).__name__}: {e}")
            elif _run_sid:
                # The run call itself raised, so `session_id` was never set: the
                # pre-generated id (when the run really took it) still names the
                # session, and the record must not be lost on this exit path.
                await _record_cut_run(job, _run_sid, idle_outcome="failed",
                                      reason="the run raised",
                                      error=f"{type(e).__name__}: {e}")
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

    store = CronJobStore(cron_db_path(data_dir))
    runner = make_agent_runner(task_agent, data_dir=data_dir)
    scheduler = CronScheduler(
        store, runner, lock_path=os.path.join(data_dir, "cron.tick.lock")
    )
    return CronTicker(scheduler, interval_seconds=interval_seconds)
