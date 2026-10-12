"""One goal run, in phases: start -> prepare -> execute -> verdict -> success (W9).

``GoalDispatcher._run_goal`` was one 638-line coroutine (131 branches). It is now a
short driver over the phases below, in the SAME order with the SAME error handling:

- ``_goal_run_start``   — heartbeat is already running; busy gate, started event,
  start notice; a cancel in this window releases what it holds and re-raises.
- ``_goal_run_prepare`` — provider/model, the run request (memory scope, pinned
  skills, buy target, resume note, room service), the unmetered-money gate, the
  pre-generated session id and the owner-authorship stamp.
- ``_goal_run_execute`` — the wall-clock-capped run; artefacts and room books are
  attributed in its ``finally``; a rail yield records nothing.
- ``_goal_run_verdict`` — refusal, declared BLOCKED, no-done salvage, all-errored,
  acceptance checks and the completion judge.
- ``_goal_run_success`` — record_success, the owner-wins re-read, promotion,
  the episode row, outcome, the owner notice and self-wake.
- ``_goal_run_failed`` / ``_goal_run_release`` — the outer ``except`` and ``finally``.

Each phase returns ``False`` where the old body returned early. A mixin of
``GoalDispatcher``; module-level names of ``agents.task.goals.dispatcher``
(``_run_task_to_outcome``, ``effective_goal_notify_on_done``, the markers, …) are
read through that module at call time, so a test that patches them there still works.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from agents.task.goals.board import Goal

# The dispatcher's logger name, so a log filter on it still sees the run's lines.
logger = logging.getLogger("agents.task.goals.dispatcher")


def _d():
    """The dispatcher module, resolved at call time (it imports this module)."""
    from agents.task.goals import dispatcher
    return dispatcher


@dataclass
class _GoalRunCtx:
    """What one goal run carries from phase to phase."""
    goal: Goal
    hb_task: Any
    shared_ws: bool = False
    payload: Dict[str, Any] = field(default_factory=dict)
    request: Dict[str, Any] = field(default_factory=dict)
    room: Any = None
    room_read_at: Any = None
    room_sid: Optional[str] = None
    pre_sid: Optional[str] = None
    max_run: Any = None
    run: Any = None
    session_id: Optional[str] = None
    recorded_success: bool = False
    final: str = ""
    outcome: Optional[str] = None
    gap_note: str = ""
    salvaged_without_done: bool = False
    judge_on: bool = False


class GoalRunMixin:
    async def _run_goal(self, goal: Goal) -> None:
        """Run one claimed goal on the task-agent core, then record + self-wake."""
        from core.config_policy import AutonomyConfig
        worker = goal.claim_lock or self._worker
        ttl = AutonomyConfig.goal_claim_ttl_sec()
        hb_task = asyncio.create_task(self._heartbeat_claim(
            goal.id, worker, ttl, claim_token=goal.claim_token,
            run_task=asyncio.current_task()))
        # 012 #1: the run envelope (ctx.run) stays reachable from the outer exception
        # handler so a failure AFTER real work still records honest steps/spend/artifacts.
        # FIX3 (T3-M1): ctx.recorded_success — once the success episode row is durably
        # recorded, a LATER incidental raise must not let the failed-write flip that
        # row's outcome from "done" back to "failed".
        ctx = _GoalRunCtx(goal=goal, hb_task=hb_task)
        await self._goal_run_start(ctx)
        try:
            if not await self._goal_run_prepare(ctx):
                return
            if not await self._goal_run_execute(ctx):
                return
            if not await self._goal_run_verdict(ctx):
                return
            await self._goal_run_success(ctx)
        except Exception as e:
            await self._goal_run_failed(ctx, e)
        finally:
            self._goal_run_release(ctx)

    async def _goal_run_start(self, ctx: _GoalRunCtx) -> None:
        """Mark the run started (busy gate on a shared workspace, event, start notice)."""
        from core.config_policy import AutonomyConfig
        goal, hb_task = ctx.goal, ctx.hb_task
        _shared_ws = False
        try:
            try:
                from agents.task.path import pm
                _shared_ws = bool(pm().is_project_root_workspace)
            except Exception:
                _shared_ws = False
            if _shared_ws:
                from core.interactive_gate import mark_busy
                mark_busy()
                self._ws_holders.add(goal.id)
            self._goal_started[goal.id] = time.monotonic()
            _d()._goal_ev(goal, "started")
            # 019 P2: tell the owner an autonomous run STARTED (not just completed/
            # digest). Rides the one delivery rail (dedup + caps); posture-gated
            # default (ON under full/autonomous), fail-open.
            if AutonomyConfig.autonomy_start_notice():
                try:
                    from core.self_evolution import push_owner_message
                    _title = (goal.title or goal.body or "")[:120]
                    # priority="low" (2026-07-20): a start ping is the least
                    # valuable thing on the rail — on 07-19 these plus other
                    # chatter spent the whole daily cap by 17:33Z, and every goal
                    # COMPLETION (with its deliverables), both digests, and the
                    # credit-sentinel halt notice were capped for the next 12h.
                    # The reserved slice keeps that headroom for what matters.
                    await push_owner_message(
                        getattr(self.task_agent, "container", None),
                        f"▶ goal started: {_title} ({goal.id[:8]})",
                        priority="low", source="lifecycle")
                except Exception:
                    logger.debug("goal start notice failed for %s", goal.id, exc_info=True)
        except asyncio.CancelledError:
            # 031: a pause cancel that lands in the start window (busy mark, start
            # notice) must release what this run already holds — the heartbeat
            # task and the shared-workspace busy gate — or both leak until restart.
            hb_task.cancel()
            if _shared_ws:
                try:
                    from core.interactive_gate import mark_idle
                    mark_idle()
                except Exception:
                    logger.debug("mark_idle after start-window cancel failed", exc_info=True)
            self._ws_holders.discard(goal.id)
            raise
        ctx.shared_ws = _shared_ws

    async def _goal_run_prepare(self, ctx: _GoalRunCtx) -> bool:
        """Build the run request; False when the goal was settled without a run."""
        from core.config_policy import AutonomyConfig
        goal, hb_task = ctx.goal, ctx.hb_task
        # An ADOPTED goal whose pinned skills changed after the owner saw
        # them runs as the agent's again (in memory; ``/adopt`` lists it).
        try:
            from agents.task.agent.skill_pins import adoption_lapsed, lapse_payload
            if adoption_lapsed(goal.payload, goal.user_id):
                logger.warning("goal %s: a pinned skill changed after /adopt — this "
                               "run is agent-authored", goal.id)
                goal.payload = lapse_payload(goal.payload or {})
        except Exception:
            logger.warning("goal %s: adopted-skill check failed", goal.id, exc_info=True)
        payload = goal.payload or {}
        from core.runtime_config import (resolve_default_provider,
                                          resolve_live_provider)
        default_provider = resolve_default_provider()[0]
        # A goal's stored provider pin is a preference, not a death pact —
        # same rule as a durable cron pin (cron/runner.resolve_job_provider).
        provider = resolve_live_provider(payload.get("provider") or default_provider)
        if provider is None:
            # 2026-08-18 intel finding: `dispatch_blocked_by_providers()` is a
            # once-per-tick, tenant-wide gate — it can pass (SOME provider is
            # alive) while THIS goal's own resolution still comes back empty
            # (its pin is dead and everything else went dead moments later,
            # e.g. mid-tick under concurrent `_run_goal` tasks). The old code
            # then fell back to `payload.get("provider") or default_provider`
            # regardless — i.e. ran anyway against a provider
            # `resolve_live_provider` had just confirmed cannot serve. Cost:
            # goal 872943753c2e burned ~$0.31 across 2 guaranteed-402 attempts
            # before its own circuit breaker tripped. Skip honestly instead —
            # same "nothing can serve" signal `dispatch_blocked_by_providers`
            # already treats as a $0 skip, just scoped to this one goal.
            hb_task.cancel()
            _g = self.board.record_failure(
                goal.id,
                error=f"{_d().LLM_EXHAUSTED_MARKER}: no live provider for this goal "
                      f"(pinned={payload.get('provider') or '(none)'!r}, "
                      f"default={default_provider!r}, both credit-dead)", claim_token=goal.claim_token)
            if getattr(_g, "claim_token", None) != goal.claim_token:
                return False
            await self._maybe_escalate_blocked(_g, block_kind_hint="provider_outage")
            return False
        # Autonomous runs have no interactive config to pick a model, so fill the
        # provider's default model from the registry when the goal doesn't pin one.
        # (A None model crashes session setup downstream — '.lower()' on None.)
        model = payload.get("model")
        if not model:
            try:
                from modules.llm.llm_client_registry import get_default_model
                model = get_default_model(provider)
            except Exception:
                model = None
        from agents.task.goals.context import build_goal_run_task
        objective = None
        if goal.parent_id:
            try:
                parent = self.board.get(goal.parent_id)
                if parent is not None and parent.kind == "objective":
                    objective = parent
            except Exception:
                objective = None
        # 057 WS-C (B9): the restart note leads the task body unconditionally
        # (it was written and read nowhere), and with GOAL_RESUME_SAME_SESSION
        # on it ALSO rides in as a SYSTEM_NOTE on the resumed session.
        _resume_note = (payload or {}).get("resume_note")
        request = {
            "task": build_goal_run_task(goal, objective,
                                        workspace_root=_d()._deliverables_root(),
                                        prior_artifacts=self._prior_artifacts(goal)),
            "provider": provider,
            "model": model,
            "tools": self._resolve_tools(goal),
            "max_steps": payload.get("max_steps", AutonomyConfig.goal_default_max_steps()),
            "temperature": 0.0,
            "goal_id": goal.id,
            # B1: recall searches the goal itself, not the restart note and
            # objective/boilerplate the run task opens with.
            "recall_seed": f"{goal.title}\n{goal.body or ''}".strip(),
        }
        # 025: the run's memory scope (goal:<root>) + regime; {} = shared.
        from agents.task.goals.memory_scope import goal_request_fields
        request.update(goal_request_fields(self.board, goal))
        # 060 WS-5: a goal pins its doctrine (`payload.skills`) — seeded into
        # the run, never left to keyword matching. Absent = byte-identical.
        from core.config_policy.rigs import pinned_skills
        _pinned_skills = pinned_skills(payload)
        if _pinned_skills:
            request["skills"] = _pinned_skills
        # 068 G2: a declared buy target rides the run as DATA; a malformed
        # one fails the run rather than silently lifting the restriction.
        from core.wallet.buy_target import PAYLOAD_KEY, normalize_target
        try:
            _target = normalize_target(payload.get(PAYLOAD_KEY))
        except ValueError as exc:
            hb_task.cancel()
            self.board.record_failure(goal.id, error=f"invalid {PAYLOAD_KEY}: {exc}", claim_token=goal.claim_token)
            return False
        if _target:
            # W0: an owner-authored goal's target is trust for this run.
            from core.wallet.buy_target import with_authorship
            request["money_target"] = with_authorship(_target, payload)
        if _resume_note:
            # Consume it: a note left on the payload would tell a LATER,
            # un-pre-empted run that it had been cut short.
            try:
                self.board.merge_payload(goal.id, {"resume_note": None})
            except Exception:
                logger.debug("resume-note clear failed for %s", goal.id,
                             exc_info=True)
            if AutonomyConfig.goal_resume_same_session() and goal.session_id:
                request["resume_session_id"] = goal.session_id
                request["resume_note"] = _resume_note
        # 044 T20: a goal carrying `payload.group` SERVICES a room — the run
        # session IS the room's bound session (PUBLIC profile + room toolset,
        # applied by bind_chat_surface), and its task is the room's ledger
        # tail since THIS reader's checkpoint. An empty tail is a $0 skip:
        # nothing was asked, so no model is paid to discover that.
        from agents.task.goals.group_service import (
            build_service_task, room_binding,
            service_read_mark,
        )
        _room = room_binding(payload)
        _room_read_at = _room_sid = None
        if _room is not None:
            _src, _key = _room
            _room_read_at = service_read_mark()
            _room_task, _skip = build_service_task(
                getattr(self.task_agent, "container", None), payload,
                owner_uid=goal.user_id,
                max_replies=AutonomyConfig.goal_group_max_replies_per_run())
            if _room_task is None:
                # There is no `record_skip` on the board, and a failure would
                # feed the circuit breaker for a room that simply went quiet
                # (or that the owner muted). The completion path with an
                # explicit, typed reason is the honest one.
                _skip = _skip or "no_change"
                logger.info("goal %s: room %s:%s — $0 skip (%s)",
                            goal.id, _src.surface_id, _src.chat_id, _skip)
                hb_task.cancel()
                self.board.record_success(
                    goal.id, result=f"skipped: {_skip} — no service work for "
                                    f"{_src.surface_id}:{_src.chat_id} this run", claim_token=goal.claim_token)
                _d()._goal_ev(goal, "skipped", _skip)
                return False
            # Pre-generate the session id so the `finally` below can still
            # find the orchestrator when a wall-clock timeout cancels the run
            # (fix round 1, Important 5), and bind WITHOUT writing the
            # chat<->session row (Critical 1: the room's live session keeps it).
            import uuid as _uuid
            _room_sid = str(_uuid.uuid4())
            request["task"] = _room_task
            request["session_source"] = _src
            request["chat_session_key"] = _key
            request["bind_write_row"] = False
            request["session_id"] = _room_sid
        # §6.2 fail-closed: a money-enabled run must not START unmetered —
        # without a database_manager, spend tracking is blind on a live
        # wallet. Clear recorded error, never a silent spend loop.
        from agents.task.runtime.metering_gate import unmetered_money_gate
        _gate_err = unmetered_money_gate(self.task_agent, request.get("tools"))
        if _gate_err is not None:
            hb_task.cancel()
            _g = self.board.record_failure(goal.id, error=_gate_err, claim_token=goal.claim_token)
            if getattr(_g, "claim_token", None) != goal.claim_token:
                return False
            await self._maybe_escalate_blocked(_g)
            return False
        # Route through the shared helper: create_session → run_session → RunOutcome.
        # H11: hard wall-clock cap (mirrors cron's per-job wait_for). max_steps alone
        # doesn't bound wall time — a single hung step would occupy a slot forever. On
        # timeout the TimeoutError is handled by the except below (record_failure) and
        # the finally cancels the claim heartbeat, so reclaim_stale can recover the slot.
        _max_run = AutonomyConfig.goal_max_run_seconds()
        # 057 WS-C (A1): pre-generate the session id for EVERY goal run (the
        # room path already did) so the `finally` below can attribute this
        # run's artefacts even when it is CANCELLED — a rail yield, an owner
        # pause or the wall-clock cap. Only when this task_agent's
        # create_session actually accepts one; a narrow fake keeps the id the
        # run reports back.
        _pre_sid = _room_sid or request.get("resume_session_id")
        if _pre_sid is None:
            try:
                from agents.task.runtime.run_as_session import create_session_accepts
                if create_session_accepts(self.task_agent.create_session, "session_id"):
                    import uuid as _uuid2
                    _pre_sid = str(_uuid2.uuid4())
                    request["session_id"] = _pre_sid
            except Exception:
                _pre_sid = None
        if _pre_sid:
            self._goal_sessions[goal.id] = _pre_sid
        # An OWNER-authored goal carries the owner's standing authority, the
        # same as an owner-authored cron job (CHAT-5): the run is recorded so
        # X posts and self-service tool loading treat it as the owner's.
        # Positive stamp only — an agent-authored or unstamped goal is not.
        try:
            from core.autonomy_marker import (
                forget_owner_goal, note_owner_goal,
            )
            if _d()._owner_authored_goal(payload):
                note_owner_goal(goal.id, "\n".join(
                    t for t in (goal.title, goal.body) if t))
            else:
                forget_owner_goal(goal.id)
        except Exception:
            logger.warning("goal %s: could not record provenance", goal.id,
                           exc_info=True)
        ctx.payload, ctx.request, ctx.max_run = payload, request, _max_run
        ctx.room, ctx.room_read_at, ctx.room_sid, ctx.pre_sid = _room, _room_read_at, _room_sid, _pre_sid
        return True

    async def _goal_run_execute(self, ctx: _GoalRunCtx) -> bool:
        """Run the request under the wall-clock cap; False when nothing is to be settled."""
        from agents.task.goals.group_service import close_room_books
        goal, hb_task, payload, request = ctx.goal, ctx.hb_task, ctx.payload, ctx.request
        _room, _room_read_at, _room_sid, _pre_sid = ctx.room, ctx.room_read_at, ctx.room_sid, ctx.pre_sid
        _max_run = ctx.max_run
        run = None
        try:
            ctx.run = run = await asyncio.wait_for(
                _d()._run_task_to_outcome(
                    self.task_agent, user_id=goal.user_id, request=request,
                    autonomous=True, goal_id=goal.id, creator="goal",
                ),
                timeout=_max_run,
            )
        finally:
            # 057 WS-C (A1): attribute the artefacts HERE, not after the
            # wait_for. `attach_goal` used to sit below, so a cancelled run
            # (yield / pause / cap) kept none of its evidence and the retry's
            # `_prior_artifacts` found nothing — "a pre-emption that discards
            # work is a bug, not a policy" (057 §2.4). Sync + fail-open, so it
            # is safe inside a finally that is unwinding a CancelledError.
            self._attach_artifacts(
                goal, getattr(run, "session_id", None) or _pre_sid)
            # 044 T20 fix round 1 (Important 5): close the room's books even
            # when the run is CANCELLED by the wall-clock cap and never
            # returns an outcome — otherwise the next tick re-answers every
            # line the timed-out run already answered.
            # Fix round 2 (N2): the REAL session id when the run produced one
            # — our pre-generated id is only honoured if create_session took
            # it, and a test fake (or a future path) may hand back another.
            if _room is not None:
                close_room_books(
                    self.task_agent, payload,
                    session_id=getattr(run, "session_id", None) or _room_sid,
                    up_to_ts=_room_read_at)
        # 057 WS-C (B8): the run ended because a rail asked it to stop at its
        # step boundary. That is a PRE-EMPTION, not an outcome: `yield_for_rail`
        # owns the board row (back to `ready`, no failure increment), so this
        # path must record nothing — recording would feed the circuit breaker
        # for work the agent was told to put down.
        if goal.id in self._yield_requested:
            logger.info("goal %s stopped at its step boundary for a rail yield",
                        goal.id)
            return False
        ctx.session_id = session_id = run.session_id
        if session_id is None:
            hb_task.cancel()
            _g = self.board.record_failure(goal.id, error="create_session returned no id", claim_token=goal.claim_token)
            if getattr(_g, "claim_token", None) != goal.claim_token:
                return False
            await self._maybe_escalate_blocked(_g)
            return False
        return True

    async def _goal_run_verdict(self, ctx: _GoalRunCtx) -> bool:
        """Judge the finished run; False when it was recorded as a failure."""
        from core.config_policy import AutonomyConfig
        goal, hb_task, payload = ctx.goal, ctx.hb_task, ctx.payload
        run, session_id = ctx.run, ctx.session_id
        # (artefact attribution moved into the wait_for's `finally` above —
        # 057 WS-C A1: a cancelled run must keep the evidence it produced.)
        # FIX 4: which of this goal's GRANTED tools never actually registered.
        # Read once, while the orchestrator is still resident, and carried into
        # both exits below — a tool-starved run must say so instead of failing
        # (or "succeeding") mutely. Empty string when nothing is missing.
        gap_note = self._tool_gap_note(session_id)
        if gap_note:
            logger.warning("goal %s ran tool-starved: %s", goal.id, gap_note)
        if run.refusal:
            # Task 10: the sentinel trip moved to error_recovery.py (the
            # universal LLM-error path) — a credit-death refusal is already
            # tripped upstream, inside the real Agent step loop, before this
            # status string is ever formed. This site now only CHECKS the
            # latch (see credit_sentinel_active() above).
            # 015 #3: a refusal whose status carries the provider-death
            # signature ("Session failed: PERMANENT ERROR: … 402 …" /
            # "ALL LLM PROVIDERS EXHAUSTED") gets the distinct marker so
            # a provider outage never hides inside the generic refusal.
            error = "run did not complete (refusal or empty)"
            block_kind_hint = None
            # Budget marker FIRST: the halt text embeds dollar amounts, and
            # a large-enough budget (e.g. "$402.00") would satisfy the
            # credit-death classifier inside _is_llm_provider_exhausted —
            # a budget halt must never be labeled a provider outage.
            if _d().RUN_BUDGET_MARKER in str(run.status or ""):
                error = f"{_d().RUN_BUDGET_MARKER}: {str(run.status)[:400]}"
            elif _d()._is_llm_provider_exhausted(run.status):
                error = f"{_d().LLM_EXHAUSTED_MARKER}: {str(run.status)[:400]}"
                block_kind_hint = "provider_outage"
            hb_task.cancel()
            _g = self.board.record_failure(
                goal.id, error=error, session_id=session_id, claim_token=goal.claim_token)
            if getattr(_g, "claim_token", None) != goal.claim_token:
                return False
            # T2.1 Task-3 review fix (finding #1, CRITICAL): this path never
            # goes through _fail_run, so it must pass the SAME classification
            # as a hint into _maybe_escalate_blocked (the single block_kind
            # stamping choke point) — otherwise a genuine provider outage
            # here lands the generic needs_input kind, which kind-aware
            # aging never auto-heals (worse than before block kinds existed).
            await self._maybe_escalate_blocked(_g, block_kind_hint=block_kind_hint)
            try:
                from modules.memory.episodic import finalize_episode
                # 012 #1: thread the envelope's real provenance (zeros for a
                # pre-loop refusal, honest values when work preceded it).
                await finalize_episode(
                    session_id=session_id, user_id=goal.user_id, kind="goal",
                    task=getattr(goal, "title", None), outcome="failed",
                    goal_id=goal.id, summary=error[:2000],
                    spend_usd=run.spend_usd, steps=run.steps,
                    artifacts=run.artifacts,
                    meta={"source": "goal"},
                )
            except Exception:
                logger.warning("goal episodic write failed", exc_info=True)
            return False
        # §2: every downstream read comes from the envelope — the done() ledger
        # text, the BLOCKED declaration, provenance — never from re-extracted
        # message-history strings (the goal-58a1385d18bf corruption).
        final = run.result_text()
        outcome = run.outcome_line
        # §3.1: an agent-declared 'OUTCOME: BLOCKED — <need>' is an honest
        # failure exit, never a success. Checked BEFORE record_success so the
        # goal routes through the breaker/escalation rail with its stated need.
        if run.blocked:
            hb_task.cancel()
            await self._fail_blocked_declared(
                goal, session_id, outcome, run.blocked_need,
                agent_reported=bool(run.user_messages),
                steps=run.steps, spend_usd=run.spend_usd,
                artifacts=run.artifacts)
            return False
        # §4.4: typed acceptance checks — optional sharpener. When a producer
        # set them they run fail-CLOSED and their results join the evidence
        # pack; nothing rejects a goal without them.
        checks = payload.get("acceptance_checks") or []
        check_results = None  # run at most ONCE (checks can have side effects)
        from agents.task.runtime.acceptance_checks import failed_checks
        # T2-01: a run that finished the loop but never called done() (max_steps
        # exhaustion, or a reply-only conversational exit) returns a non-refusal
        # status string that looks identical to a genuine completion. Recording it
        # as board success was the prod "marked done, never posted" failure. Only a
        # POSITIVE "ran but no done()" (False) routes to the failure/escalation
        # rail — None (undeterminable) falls through to the legacy path unchanged.
        #
        # FIX 6: ...UNLESS the goal declared typed acceptance checks and every one
        # passes. The producer's own definition of done is stronger evidence than
        # the missing done() call, so a run that exhausted max_steps while
        # PRODUCING every declared deliverable is scored by what it produced
        # (proposed by the 2026-08-20 review; open since). The failure path is
        # untouched for a failing check, for a goal with no checks, and for
        # `all_actions_errored` (nothing executed successfully, so the
        # deliverables cannot be this run's work — the §4.2 invariant below).
        salvaged_without_done = False
        if run.done_called is False:
            if checks and not run.all_actions_errored:
                check_results = await self._evaluate_acceptance(
                    checks, goal, session_id, run)
            _failed = failed_checks(check_results) if check_results else None
            if check_results and not _failed:
                salvaged_without_done = True
            else:
                detail = ""
                if _failed:
                    detail = "; acceptance checks failed: " + "; ".join(
                        f"{c.get('type')}: {c.get('detail')}" for c in _failed)
                hb_task.cancel()
                await self._fail_run(
                    goal, session_id,
                    error=("run ended without completing (no done() — likely ran "
                           "out of steps)" + detail + gap_note)[:2000],
                    outcome=outcome,
                    steps=run.steps, spend_usd=run.spend_usd,
                    artifacts=run.artifacts)
                return False
        # §4.2 NEW invariant: a done() where EVERY substantive action errored
        # is not a judgment call — nothing executed successfully, so the claim
        # has no basis. Deterministic, needs no goal semantics.
        if run.all_actions_errored:
            hb_task.cancel()
            await self._fail_run(
                goal, session_id,
                error=("done() after every action errored — nothing executed "
                       "successfully" + gap_note)[:2000],
                outcome=outcome,
                steps=run.steps, spend_usd=run.spend_usd,
                artifacts=run.artifacts)
            return False
        if checks and check_results is None:
            check_results = await self._evaluate_acceptance(
                checks, goal, session_id, run)
        if check_results:
            _failed = failed_checks(check_results)
            if _failed:
                hb_task.cancel()
                await self._fail_run(
                    goal, session_id,
                    error=("acceptance checks failed: " + "; ".join(
                        f"{c.get('type')}: {c.get('detail')}" for c in _failed)
                        + gap_note)[:2000],
                    outcome=outcome,
                    steps=run.steps, spend_usd=run.spend_usd,
                    artifacts=run.artifacts)
                return False
        # §4.3 evidence-grounded completion review (autonomous runs): the
        # CLAIM read against the evidence pack — no acceptance required.
        # unmet (claim contradicted by evidence) -> failure with the gap;
        # met -> verified; unclear/error/timeout -> done (UNVERIFIED) — the
        # run completes (a framework for arbitrary goals must not hard-block
        # fuzzy work) but the label is honest everywhere and learning loops
        # do not consume it.
        import agents.task.goals.completion_judge as _cj
        judge_on = AutonomyConfig.goal_completion_judge()
        # FIX 6: a salvaged (no-done()) run has NO completion claim, and the
        # judge exists to weigh a claim against the evidence. The typed checks
        # already passed deterministically — that IS the verdict, so skip the
        # (paid) judge rather than let it invent an UNMET for a missing claim.
        if judge_on and not salvaged_without_done:
            verdict, reason = await _cj.judge_run_outcome(
                self.task_agent, session_id, goal, run)
            if verdict == _cj.VERDICT_UNMET:
                hb_task.cancel()
                await self._fail_run(
                    goal, session_id,
                    error=(f"completion judge: {reason} "
                           f"(claim contradicted by evidence)" + gap_note)[:2000],
                    outcome=outcome,
                    steps=run.steps, spend_usd=run.spend_usd,
                    artifacts=run.artifacts)
                return False
            run.verified = "verified" if verdict == _cj.VERDICT_MET else "unverified"
        elif salvaged_without_done:
            run.verified = "verified"  # every declared acceptance check passed
        ctx.final, ctx.outcome, ctx.gap_note = final, outcome, gap_note
        ctx.salvaged_without_done, ctx.judge_on = salvaged_without_done, judge_on
        return True

    async def _goal_run_success(self, ctx: _GoalRunCtx) -> None:
        """Record an accepted completion and tell whoever needs to know."""
        from core.config_policy import AutonomyConfig
        goal, hb_task = ctx.goal, ctx.hb_task
        run, session_id = ctx.run, ctx.session_id
        final, outcome, gap_note = ctx.final, ctx.outcome, ctx.gap_note
        salvaged_without_done, judge_on = ctx.salvaged_without_done, ctx.judge_on
        # Honest result recording: the envelope filters placeholders/status
        # strings, so an empty result_text means the run genuinely produced no
        # recoverable text — say THAT instead of shipping a placeholder.
        result_record = final or "(completed via done() — no textual output captured)"
        if salvaged_without_done:
            # Never dress this up as a normal completion: the run stopped
            # without declaring done(); it is accepted because every typed
            # acceptance check the goal declared passed.
            result_record = (
                (final or "(no textual output captured)")
                + "\n\n(completed without an explicit done() — accepted because "
                  "every declared acceptance check passed)")
        if gap_note:
            result_record = f"{result_record}\n\n{gap_note.strip()}"
        hb_task.cancel()
        accepted = self.board.record_success(
            goal.id, session_id=session_id, result=result_record[:4000],
            claim_token=goal.claim_token)
        if accepted is False:
            return
        # Stale-completion skip: an owner may have cancelled/paused the goal
        # mid-run (T2 guards keep that status through record_success — see
        # test_intervention_guards.py). Re-read the row; if it isn't 'done'
        # the owner's decision wins — never write an outcome or self-wake for
        # a run they already walked away from. Fail-open (get() error ->
        # proceed) so a board without a working get() (fakes in older tests)
        # doesn't lose success recording.
        try:
            refreshed = self.board.get(goal.id)
        except Exception:
            refreshed = None
        if refreshed is not None and refreshed.status != _d().STATUS_DONE:
            return
        # 025: a ROOT goal's quarantined findings graduate to shared recall only
        # now — after the owner-wins re-read, and only when verified.
        from agents.task.goals.memory_scope import promote_after_success
        _promoted = await asyncio.to_thread(  # sqlite: never on the shared loop
            promote_after_success, self.board, goal,
            verified=run.verified if judge_on else "verified")
        try:
            from modules.memory.episodic import finalize_episode
            # Provenance was collected into the envelope while the
            # orchestrator was resident (run_task_to_outcome).
            await finalize_episode(
                session_id=session_id, user_id=goal.user_id, kind="goal",
                task=getattr(goal, "title", None), outcome="done", goal_id=goal.id,
                summary=result_record[:2000],
                spend_usd=run.spend_usd, steps=run.steps,
                artifacts=run.artifacts,
                meta={"source": "goal", "verified": run.verified},
            )
            ctx.recorded_success = True
            _d()._goal_ev(goal, "done", session_id=session_id,
                     spend_usd=run.spend_usd, steps=run.steps,
                     # 057 WS-C (B10): the pre-flight's ONLY input. Without a
                     # runtime on the terminal event there is nothing to take
                     # a p95 of, and the estimate can only ever be a guess.
                     duration_sec=self._elapsed(goal.id),
                     artifacts=len(run.artifacts),
                     user_messages=len(run.user_messages),
                     verified=run.verified, promoted=_promoted)
        except Exception:
            logger.warning("goal episodic write failed", exc_info=True)
        if outcome:
            try:
                self.board.set_outcome(goal.id, outcome)
            except Exception:
                logger.debug("set_outcome failed for %s", goal.id, exc_info=True)
        # Tell the OWNER a background goal COMPLETED — decoupled from the
        # (posture-gated, server-default-OFF) self-wake re-entry so completions are
        # reported even when self-wake is off. The owner-push previously lived ONLY
        # inside _self_wake, so on the server completed goals told no one (the
        # "goals never report completions" gap). Cheap ($0), controllable.
        # §3.4: the completion push is a SAFETY NET, not the voice — the
        # agent's own send_message (delivered live via the §3.1 rail) is the
        # primary channel. The framework only speaks when the agent said
        # nothing to its user during the run.
        if _d().effective_goal_notify_on_done(goal.user_id, self._home_dir()) \
                and not run.user_messages:
            await self._notify_owner_done(goal, session_id, result_record,
                                          verified=run.verified if judge_on else "verified",
                                          artifacts=run.artifacts, promoted=_promoted)
        # §4.3: an UNVERIFIED completion earns nothing downstream — no
        # self-wake re-entry. With the judge disabled, legacy behavior holds.
        if AutonomyConfig.goal_self_wake_enabled() and \
                (not judge_on or run.verified == "verified"):
            await self._self_wake(goal, session_id, result_record)

    async def _goal_run_failed(self, ctx: _GoalRunCtx, e: Exception) -> None:
        """The outer ``except``: record the raise as this run's failure."""
        goal, hb_task = ctx.goal, ctx.hb_task
        run, session_id, recorded_success = ctx.run, ctx.session_id, ctx.recorded_success
        # 057 WS-C (B8): same rule as the clean-exit path — a run torn down for
        # a rail yield is never recorded as this goal's failure.
        if goal.id in self._yield_requested:
            logger.info("goal %s ended for a rail yield (%s)", goal.id,
                        type(e).__name__)
            return
        logger.error("goal %s run failed: %s", goal.id, e, exc_info=True)
        # 015 #3: a permanent LLM/provider death (OpenRouter 402 →
        # LLMPermanentError → "ALL LLM PROVIDERS EXHAUSTED") gets the
        # distinct greppable marker in goals.last_failure_error.
        error_text = str(e)
        block_kind_hint = None
        if _d()._is_llm_provider_exhausted(e):
            error_text = f"{_d().LLM_EXHAUSTED_MARKER}: {error_text}"[:2000]
            block_kind_hint = "provider_outage"
        _d()._goal_ev(goal, "failed", reason=error_text[:200], session_id=session_id,
                 steps=int(getattr(run, "steps", 0) or 0),
                 duration_sec=self._elapsed(goal.id))
        try:
            hb_task.cancel()
            _g = self.board.record_failure(goal.id, error=error_text, session_id=session_id, claim_token=goal.claim_token)
            if getattr(_g, "claim_token", None) != goal.claim_token:
                return
            # T2.1 Task-3 review fix (finding #1, CRITICAL): this path also
            # never goes through _fail_run — same hint mechanism, same
            # rationale as the refusal-status site above.
            await self._maybe_escalate_blocked(_g, block_kind_hint=block_kind_hint)
        except Exception:
            pass
        if session_id and not recorded_success:
            try:
                from modules.memory.episodic import finalize_episode
                # 012 #1: thread whatever provenance the run envelope already
                # computed (run stays None when the raise preceded run_session,
                # in which case the safe zeros are the honest values).
                await finalize_episode(
                    session_id=session_id, user_id=goal.user_id, kind="goal",
                    task=getattr(goal, "title", None), outcome="failed",
                    goal_id=goal.id, summary=error_text[:2000],
                    spend_usd=float(getattr(run, "spend_usd", 0.0) or 0.0),
                    steps=int(getattr(run, "steps", 0) or 0),
                    artifacts=list(getattr(run, "artifacts", None) or []),
                    meta={"source": "goal"},
                )
            except Exception:
                logger.warning("goal episodic write failed", exc_info=True)

    def _goal_run_release(self, ctx: _GoalRunCtx) -> None:
        """The outer ``finally``: stop the heartbeat, release the busy gate and slot."""
        goal, hb_task = ctx.goal, ctx.hb_task
        hb_task.cancel()
        if ctx.shared_ws:
            from core.interactive_gate import mark_idle
            mark_idle()
        current = self._goal_tasks.get(goal.id)
        if current is None or current is asyncio.current_task():
            self._ws_holders.discard(goal.id)
            self._goal_started.pop(goal.id, None)

    async def _evaluate_acceptance(self, checks: list, goal: Goal,
                                   session_id: Optional[str], run) -> list:
        """Run the goal's typed acceptance checks ONCE and file them as evidence.

        Extracted (FIX 6) so the no-``done()`` salvage path and the normal
        post-``done()`` path share one execution — a check can touch the network
        or the ledger, so it must never run twice for one goal run.
        """
        _ws = None
        try:
            from agents.task.runtime.evidence import _resolve_workspace_dir
            _ws = _resolve_workspace_dir(self.task_agent.get_orchestrator(session_id))
        except Exception:
            _ws = None
        from agents.task.runtime.acceptance_checks import run_acceptance_checks
        # Thread tenant + goal so an `artifact` check can resolve through
        # the ledger rather than a workspace-relative path.
        check_results = await run_acceptance_checks(
            checks, workspace_dir=_ws, user_id=goal.user_id,
            goal_id=goal.id, session_id=session_id)
        if getattr(run, "evidence", None) is not None:
            try:
                run.evidence.checks = check_results
            except Exception:
                pass
        return check_results

    def _tool_gap_note(self, session_id: Optional[str]) -> str:
        """FIX 4: the run's requested-but-unregistered tools, as ONE prefixed line
        (``"\\n[tool gap] …"``), or ``""``.

        Read duck-typed off the session's Controller
        (``tools/controller/tool_management.py::tool_gap_note``) — the agents tier
        must not import the tools tier (layering ratchet), and an orchestrator or
        controller without the probe (dead session, foreign object) is simply
        silent. Fail-open: bookkeeping never fails a finished run.
        """
        try:
            orch = self.task_agent.get_orchestrator(session_id)
            probe = getattr(getattr(orch, "controller", None), "tool_gap_note", None)
            note = probe() if callable(probe) else ""
            return f"\n{note}" if note else ""
        except Exception:
            logger.debug("tool-gap probe skipped for session %s", session_id,
                         exc_info=True)
            return ""

    async def _fail_blocked_declared(self, goal: Goal, session_id: Optional[str],
                                     outcome: Optional[str], need: str,
                                     agent_reported: bool = False,
                                     steps: int = 0, spend_usd: float = 0.0,
                                     artifacts: Optional[list] = None) -> None:
        """§3.1: route an agent-declared BLOCKED outcome to the failure/escalation rail.

        The agent already concluded retrying won't help, so after the standard
        record_failure (whose CAS respects owner cancel/pause) a row that came back
        'ready' is flipped straight to 'blocked' — skipping the breaker's remaining
        retries. A non-ready row means the owner intervened; their decision wins.
        ``steps``/``spend_usd``/``artifacts`` thread the run's real provenance
        through to the episodes row (012 #1).
        """
        error = f"agent declared BLOCKED: {need or 'unspecified need'}"
        await self._fail_run(goal, session_id, error=error, block=True, outcome=outcome,
                             agent_reported=agent_reported,
                             steps=steps, spend_usd=spend_usd, artifacts=artifacts)

    async def _fail_run(self, goal: Goal, session_id: Optional[str], *, error: str,
                        block: bool = False, outcome: Optional[str] = None,
                        agent_reported: bool = False,
                        steps: int = 0, spend_usd: float = 0.0,
                        artifacts: Optional[list] = None) -> None:
        """Shared verified-failure path for a run that finished but didn't deliver.

        record_failure's CAS respects owner cancel/pause; with ``block=True`` a row
        that came back 'ready' is flipped straight to 'blocked' (skipping the
        breaker's remaining retries — used when retrying provably won't help).

        012 #1: ``steps``/``spend_usd``/``artifacts`` carry the RunOutcome's real
        provenance into the episodes row — a run that did real work before being
        classified failed must not be recorded steps=0/spend=0. Optional with safe
        zero defaults so a call site without an envelope still works.
        """
        _g = self.board.record_failure(goal.id, error=error, session_id=session_id, claim_token=goal.claim_token)
        if getattr(_g, "claim_token", None) != goal.claim_token:
            return  # a late failure must not block or publish for a newer claim
        if block and getattr(_g, "status", None) == _d().STATUS_READY:
            try:
                if self.board.block_from_ready(goal.id, error=error):
                    _g = self.board.get(goal.id) or _g
            except Exception:
                logger.debug("block_from_ready failed for %s", goal.id, exc_info=True)
        # T2.1 Task 3 (review-fixed, finding #1): classify the failure ONCE (do
        # not re-derive downstream) and pass it as a HINT into
        # _maybe_escalate_blocked — the SINGLE block_kind stamping choke point.
        # Stamping directly here (the original shape) was a SECOND, inconsistent
        # mechanism: the refusal-status and top-level exception call sites in
        # _run_goal never route through _fail_run at all (they call
        # record_failure/_maybe_escalate_blocked directly), so a real provider
        # death reaching THOSE call sites was silently mis-stamped the generic
        # `needs_input` instead — worse than pre-block-kinds behavior, since
        # kind-aware `age_out_blocked` never auto-heals `needs_input`. One
        # mechanism, used everywhere `_maybe_escalate_blocked` is called.
        block_kind_hint = "provider_outage" if _d()._is_llm_provider_exhausted(error) else None
        # T4-03: surface the verified-failure outcome (blocked vs failed) in the durable
        # event log so `polyrob telemetry` reflects it, not just the episodes table.
        _d()._goal_ev(goal, "blocked" if getattr(_g, "status", None) == _d().STATUS_BLOCKED else "failed",
                 reason=str(error)[:200], session_id=session_id)
        if outcome:
            try:
                self.board.set_outcome(goal.id, outcome)
            except Exception:
                logger.debug("set_outcome failed for %s", goal.id, exc_info=True)
        await self._maybe_escalate_blocked(_g, agent_reported=agent_reported,
                                           block_kind_hint=block_kind_hint)
        if session_id:
            try:
                from modules.memory.episodic import finalize_episode
                await finalize_episode(
                    session_id=session_id, user_id=goal.user_id, kind="goal",
                    task=getattr(goal, "title", None), outcome="failed",
                    goal_id=goal.id, summary=error[:2000],
                    spend_usd=spend_usd, steps=steps,
                    artifacts=list(artifacts or []),
                    meta={"source": "goal"},
                )
            except Exception:
                logger.warning("goal episodic write failed", exc_info=True)
