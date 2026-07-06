"""Goal dispatcher + ticker (W4).

Turns ``ready`` goals on the board into agent runs, bounded by concurrency, safe
under ``workers>1`` (the cron ``TickLock`` gates a tick across processes), and
feeding completions back to the W1 self-wake rail so a finished goal can forge a
follow-up turn.

Mirrors ``cron/runner.py``'s shape: a pure ``GoalDispatcher.dispatch_once`` (unit
tested with a fake board/agent) + a ``GoalTicker.run_forever`` loop the FastAPI
lifespan starts/stops exactly like the cron ticker. Gated ``GOALS_ENABLED``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from agents.task.goals.board import GoalBoard, Goal, STATUS_DONE, STATUS_READY
from agents.task.runtime.run_as_session import run_task_as_session as _run_task_as_session

logger = logging.getLogger(__name__)

# Tools a self-decomposed CHILD goal may inherit from its parent when it set none.
# Deliberately excludes money/social/trading tools (wallet, x402, hyperliquid,
# polymarket, twitter) so the agent can't self-grant spend/post capability by
# spawning a child goal. Server-side allowlist, NOT agent-controllable.
CHILD_INHERITABLE_TOOLS = frozenset(
    {"filesystem", "task", "browser", "perplexity", "mcp", "anysite", "coding"}
)

# Safe default toolset when a goal sets no tools and has nothing inheritable.
_DEFAULT_GOAL_TOOLS = ["filesystem", "task"]


def effective_goal_concurrency() -> int:
    """Goal in-flight cap, clamped to single-flight on a SHARED project folder.

    When the installed pm() serves one project-root workspace (CLI/headless project
    mode), concurrent goal runs would interleave read-modify-write edits on the same
    files (the battle-test "read INDEX.md, append" corruption). Serialize them.
    Keyed off the installed pm() — NOT an env var — so the multi-tenant server (whose
    global pm() is per-session) keeps its full GOAL_MAX_CONCURRENT (MT-5). Fail-open
    to the unclamped cap on any pm() error.
    """
    from agents.task.constants import AutonomyConfig
    cap = AutonomyConfig.goal_max_concurrent()
    try:
        from agents.task.path import pm
        if pm().is_project_root_workspace:
            return min(cap, 1)
    except Exception:
        pass
    return cap


def _heartbeat_interval(ttl: int) -> int:
    """Heartbeat cadence for an in-flight goal claim: ttl/3, floored at 30s (F8).

    Pinging at a third of the TTL gives two missed-beat margins before a claim
    would expire, while staying infrequent enough to be cheap.
    """
    return max(30, int(ttl) // 3)


class GoalDispatcher:
    def __init__(self, board: GoalBoard, task_agent: Any, *, lock_path: Optional[str] = None):
        self.board = board
        self.task_agent = task_agent
        self.lock_path = lock_path
        # Strong refs to in-flight goal runs — without this an unawaited create_task
        # can be GC'd mid-run (CPython drops weakly-referenced tasks), cancelling a
        # goal that "may run minutes". Cleared via done-callback.
        self._inflight: set = set()
        self._quota_logged = False
        # §7.2 tail: consecutive planner runs that left the ready queue empty, and
        # whether the resulting stall was already escalated (escalate ONCE per stall;
        # both reset the moment the board refills). In-memory: resets on restart,
        # which at worst re-escalates one stall after a deploy — acceptable.
        self._empty_planner_runs = 0
        self._empty_pipeline_escalated = False

    async def dispatch_once(self) -> int:
        """Claim and run up to GOAL_MAX_CONCURRENT ready goals. Returns #dispatched.

        Cross-process safe: a single tick is gated by the cron-style TickLock so two
        workers don't both fan out the same ready set. ``reclaim_stale`` runs FIRST
        and UNCONDITIONALLY — before the ``GOALS_ENABLED`` gate (AU-F4.3) — so a
        crashed ``running`` row isn't stuck forever if the flag was flipped off.
        """
        from agents.task.constants import AutonomyConfig

        # AU-F4.3: reclaim expired claims BEFORE the enabled-gate so a crashed
        # `running` row doesn't stay stuck forever just because GOALS_ENABLED was
        # (temporarily) flipped off. reclaim_stale is a self-contained retry-safe SQL
        # UPDATE (agents/task/goals/board.py) -- it needs no external tick-lock /
        # workspace-lock precondition, so it's safe to run unconditionally here.
        try:
            self.board.reclaim_stale()
        except Exception:
            logger.warning("goal dispatch: reclaim_stale failed", exc_info=True)

        if not AutonomyConfig.goals_enabled():
            return 0

        from core.interactive_gate import is_interactive_busy
        if is_interactive_busy():
            return 0  # a human is mid-turn; don't run a goal in the shared workspace

        lock = None
        if self.lock_path:
            from cron.scheduler import TickLock
            lock = TickLock(self.lock_path)
            if not lock.acquire():
                return 0
        # C2: gate the dispatch decision behind the cross-process workspace lock too,
        # so a 2nd `rob` process doesn't start goals in the shared CWD while another
        # process's REPL is mid-turn. Non-blocking; defer the tick if contended.
        from core.interactive_gate import workspace_turn_lock
        _ws_lock = workspace_turn_lock(timeout=0)
        try:
            _ws_lock.__enter__()
        except Exception:
            # Fail-open: any workspace-lock contention/error defers the tick.
            if lock is not None:
                lock.release()
            return 0
        try:
            # (reclaim_stale already ran above, before the enabled-gate — AU-F4.3)
            limit = effective_goal_concurrency()
            # GOAL_MAX_CONCURRENT is a global in-flight cap, NOT a per-tick claim
            # quota. Subtract goals ALREADY RUNNING from the cross-process DB count
            # (not the per-process self._inflight, which can't see other workers'
            # runs) so a backlog can't grow concurrent agent sessions without bound
            # — under workers>1 the per-process count let total reach cap x workers.
            try:
                running = self.board.count_running()
            except Exception:
                running = len(self._inflight)  # fail-open to per-process count
            slots = max(0, limit - running)
            quota = AutonomyConfig.goal_daily_quota()
            if quota > 0:
                try:
                    used = self.board.count_started_since(86400)
                except Exception:
                    used = 0  # fail-open: never let quota accounting kill dispatch
                headroom = max(0, quota - used)
                if headroom == 0:
                    if not self._quota_logged:
                        logger.warning(
                            "goal daily quota exhausted (%d started/24h >= %d) — pausing dispatch",
                            used, quota)
                        self._quota_logged = True
                    return 0
                self._quota_logged = False
                slots = min(slots, headroom)
            if slots == 0:
                return 0
            ready = self.board.ready(limit=slots)
            ttl = AutonomyConfig.goal_claim_ttl_sec()
            worker = f"goal-dispatch-{os.getpid()}"
            dispatched = 0
            for g in ready:
                claimed = self.board.claim(g.id, worker, ttl_seconds=ttl)
                if claimed is None:
                    continue  # another worker won the race
                t = asyncio.create_task(self._run_goal(claimed))
                self._inflight.add(t)
                t.add_done_callback(self._inflight.discard)
                dispatched += 1
            # Fire-and-forget: the ticker doesn't block on goal completion (goals may
            # run minutes). The runs self-report via record_success/failure + self-wake.
            try:
                await self._maybe_plan(headroom_after=slots - dispatched)
            except Exception:
                logger.debug("planner check failed (non-fatal)", exc_info=True)
            return dispatched
        finally:
            try:
                _ws_lock.__exit__(None, None, None)
            except Exception:
                pass
            if lock is not None:
                lock.release()

    async def _heartbeat_claim(self, goal_id: str, worker: str, ttl: int) -> None:
        """Keep a long-running goal's claim alive (F8).

        Goals run fire-and-forget for up to many minutes, but the claim TTL
        (GOAL_CLAIM_TTL_SEC, default 900s) is fixed at claim time. Without a
        heartbeat, a goal that runs longer than the TTL is reclaimed by
        ``reclaim_stale`` and DOUBLE-dispatched. Ping the claim every ttl/3.
        """
        interval = _heartbeat_interval(ttl)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    self.board.heartbeat(goal_id, worker, ttl_seconds=ttl)
                except Exception as e:  # never let heartbeat kill the run
                    logger.warning("goal %s heartbeat failed: %s", goal_id, e)
        except asyncio.CancelledError:
            pass

    async def _run_goal(self, goal: Goal) -> None:
        """Run one claimed goal on the task-agent core, then record + self-wake."""
        session_id = None
        # FIX3 (T3-M1): once the success episode row is durably recorded, a LATER
        # incidental raise in this same try block (e.g. extract_outcome_line,
        # self-wake) must not let the outer except's failed-write flip that row's
        # outcome from "done" back to "failed" — see the guard at the bottom.
        recorded_success = False
        from agents.task.constants import AutonomyConfig
        worker = f"goal-dispatch-{os.getpid()}"
        ttl = AutonomyConfig.goal_claim_ttl_sec()
        hb_task = asyncio.create_task(self._heartbeat_claim(goal.id, worker, ttl))
        # C6: on a SHARED project-root workspace (local/CLI), hold the in-process
        # busy gate for the RUN's full duration — not just the dispatch decision.
        # dispatch_once acquires the gate only to CLAIM, then releases it before this
        # fire-and-forget run; without this, a cron tick or the next goal dispatch
        # would start a second file-mutating run in the SAME CWD concurrently. The
        # server (per-session workspaces) does not clamp, so this is scoped to the
        # shared-workspace case to avoid needlessly serializing server throughput.
        # (NB: a live REPL turn does not consult this gate, so goal-vs-live-turn on
        # the shared CWD is still only fully closed once the REPL waits on it.)
        _shared_ws = False
        try:
            from agents.task.path import pm
            _shared_ws = bool(pm().is_project_root_workspace)
        except Exception:
            _shared_ws = False
        if _shared_ws:
            from core.interactive_gate import mark_busy
            mark_busy()
        try:
            payload = goal.payload or {}
            from core.runtime_config import resolve_runtime_config
            default_provider = resolve_runtime_config(None, None)[0]
            provider = payload.get("provider") or default_provider
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
            request = {
                "task": build_goal_run_task(goal, objective),
                "provider": provider,
                "model": model,
                "tools": self._resolve_tools(goal),
                "max_steps": payload.get("max_steps", 20),
                "temperature": 0.0,
                "goal_id": goal.id,
            }
            # Route through the shared helper: create_session → run_session → refusal-check.
            # H11: hard wall-clock cap (mirrors cron's per-job wait_for). max_steps alone
            # doesn't bound wall time — a single hung step would occupy a slot forever. On
            # timeout the TimeoutError is handled by the except below (record_failure) and
            # the finally cancels the claim heartbeat, so reclaim_stale can recover the slot.
            _max_run = AutonomyConfig.goal_max_run_seconds()
            session_id, final = await asyncio.wait_for(
                _run_task_as_session(
                    self.task_agent, user_id=goal.user_id, request=request, autonomous=True
                ),
                timeout=_max_run,
            )
            # Back-half: goal-specific record calls (no is_refusal needed — helper normalised).
            if session_id is None:
                _g = self.board.record_failure(goal.id, error="create_session returned no id")
                await self._maybe_escalate_blocked(_g)
                return
            if final is None:
                _g = self.board.record_failure(
                    goal.id,
                    error="run did not complete (refusal or empty)",
                    session_id=session_id,
                )
                await self._maybe_escalate_blocked(_g)
                try:
                    from modules.memory.episodic import finalize_episode
                    await finalize_episode(
                        session_id=session_id, user_id=goal.user_id, kind="goal",
                        task=getattr(goal, "title", None), outcome="failed",
                        goal_id=goal.id, meta={"source": "goal"},
                    )
                except Exception:
                    logger.warning("goal episodic write failed", exc_info=True)
                return
            # §3.1: an agent-declared 'OUTCOME: BLOCKED — <need>' is an honest
            # failure exit, never a success. Checked BEFORE record_success so the
            # goal routes through the breaker/escalation rail with its stated need.
            from agents.task.goals.context import extract_outcome_line, parse_blocked_outcome
            # Fail-open: a crash in the outcome PARSER must never fail a completed
            # run (FIX3 — the success path below still records honestly).
            try:
                outcome = extract_outcome_line(final)
                blocked_need = parse_blocked_outcome(outcome)
            except Exception:
                logger.debug("outcome parse failed for %s", goal.id, exc_info=True)
                outcome, blocked_need = None, None
            if blocked_need is not None:
                await self._fail_blocked_declared(goal, session_id, outcome, blocked_need)
                return
            # §3.2 (fail-open — never block on uncertainty): with the judge on and
            # an acceptance set, 'unmet' -> record_failure (normal breaker retries);
            # 'met'/'unclear'/error -> the legacy success path.
            import agents.task.goals.completion_judge as _cj
            acceptance = (payload.get("acceptance") or "").strip()
            if AutonomyConfig.goal_completion_judge() and acceptance:
                verdict, reason = await _cj.judge_goal_completion(
                    self.task_agent, session_id, goal, final)
                if verdict == "unmet":
                    await self._fail_run(goal, session_id,
                                         error=f"completion judge: {reason}"[:2000],
                                         outcome=outcome)
                    return
            self.board.record_success(goal.id, session_id=session_id, result=str(final)[:4000])
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
            if refreshed is not None and refreshed.status != STATUS_DONE:
                return
            try:
                from modules.memory.episodic import finalize_episode, collect_provenance
                orchestrator = self.task_agent.get_orchestrator(session_id)
                prov = await collect_provenance(orchestrator)
                await finalize_episode(
                    session_id=session_id, user_id=goal.user_id, kind="goal",
                    task=getattr(goal, "title", None), outcome="done", goal_id=goal.id,
                    summary=str(final)[:2000] if final is not None else None,
                    spend_usd=prov["spend_usd"], steps=prov["steps"],
                    artifacts=prov["artifacts"], meta={"source": "goal"},
                )
                recorded_success = True
            except Exception:
                logger.warning("goal episodic write failed", exc_info=True)
            if outcome:
                try:
                    self.board.set_outcome(goal.id, outcome)
                except Exception:
                    logger.debug("set_outcome failed for %s", goal.id, exc_info=True)
            if AutonomyConfig.goal_self_wake_enabled():
                await self._self_wake(goal, session_id, final)
        except Exception as e:
            logger.error("goal %s run failed: %s", goal.id, e, exc_info=True)
            try:
                _g = self.board.record_failure(goal.id, error=str(e), session_id=session_id)
                await self._maybe_escalate_blocked(_g)
            except Exception:
                pass
            if session_id and not recorded_success:
                try:
                    from modules.memory.episodic import finalize_episode
                    await finalize_episode(
                        session_id=session_id, user_id=goal.user_id, kind="goal",
                        task=getattr(goal, "title", None), outcome="failed",
                        goal_id=goal.id, summary=str(e)[:2000],
                        meta={"source": "goal"},
                    )
                except Exception:
                    logger.warning("goal episodic write failed", exc_info=True)
        finally:
            hb_task.cancel()
            if _shared_ws:
                from core.interactive_gate import mark_idle
                mark_idle()

    async def _fail_blocked_declared(self, goal: Goal, session_id: Optional[str],
                                     outcome: Optional[str], need: str) -> None:
        """§3.1: route an agent-declared BLOCKED outcome to the failure/escalation rail.

        The agent already concluded retrying won't help, so after the standard
        record_failure (whose CAS respects owner cancel/pause) a row that came back
        'ready' is flipped straight to 'blocked' — skipping the breaker's remaining
        retries. A non-ready row means the owner intervened; their decision wins.
        """
        error = f"agent declared BLOCKED: {need or 'unspecified need'}"
        await self._fail_run(goal, session_id, error=error, block=True, outcome=outcome)

    async def _fail_run(self, goal: Goal, session_id: Optional[str], *, error: str,
                        block: bool = False, outcome: Optional[str] = None) -> None:
        """Shared verified-failure path for a run that finished but didn't deliver.

        record_failure's CAS respects owner cancel/pause; with ``block=True`` a row
        that came back 'ready' is flipped straight to 'blocked' (skipping the
        breaker's remaining retries — used when retrying provably won't help).
        """
        _g = self.board.record_failure(goal.id, error=error, session_id=session_id)
        if block and getattr(_g, "status", None) == STATUS_READY:
            try:
                if self.board.block_from_ready(goal.id, error=error):
                    _g = self.board.get(goal.id) or _g
            except Exception:
                logger.debug("block_from_ready failed for %s", goal.id, exc_info=True)
        if outcome:
            try:
                self.board.set_outcome(goal.id, outcome)
            except Exception:
                logger.debug("set_outcome failed for %s", goal.id, exc_info=True)
        await self._maybe_escalate_blocked(_g)
        if session_id:
            try:
                from modules.memory.episodic import finalize_episode
                await finalize_episode(
                    session_id=session_id, user_id=goal.user_id, kind="goal",
                    task=getattr(goal, "title", None), outcome="failed",
                    goal_id=goal.id, summary=error[:2000], meta={"source": "goal"},
                )
            except Exception:
                logger.warning("goal episodic write failed", exc_info=True)

    def _resolve_tools(self, goal: Goal) -> list:
        """Resolve the toolset for a goal run.

        Precedence: the goal's own ``payload.tools`` if set; else, for a CHILD goal
        (has ``parent_id``), inherit the parent's tools intersected with
        ``CHILD_INHERITABLE_TOOLS`` (so a self-decomposed goal isn't tool-starved
        while never self-granting money/social tools); else the safe default.
        """
        payload = goal.payload or {}
        own = payload.get("tools")
        if own:
            return own
        if goal.parent_id:
            try:
                parent = self.board.get(goal.parent_id)
            except Exception:
                parent = None
            if parent is not None:
                parent_tools = (parent.payload or {}).get("tools") or []
                inherited = [t for t in parent_tools if t in CHILD_INHERITABLE_TOOLS]
                if inherited:
                    return inherited
        return list(_DEFAULT_GOAL_TOOLS)

    async def _maybe_escalate_blocked(self, goal) -> None:
        """§7.2: when record_failure tripped the breaker (goal now 'blocked'), surface
        a concrete ask to the owner instead of letting it die silently. Fail-open."""
        try:
            from agents.task.goals.escalation import maybe_escalate_blocked
            await maybe_escalate_blocked(self.task_agent, goal)
        except Exception:
            logger.debug("blocker escalation skipped", exc_info=True)
        # §7.2b: regardless of whether the push reached the owner, leave a TRACKED
        # ask on the board so the need survives (and `owner fulfill` can unblock).
        try:
            from agents.task.constants import AutonomyConfig
            from agents.task.goals.board import STATUS_BLOCKED
            if (AutonomyConfig.goal_blocker_escalation()
                    and getattr(goal, "status", None) == STATUS_BLOCKED):
                self.board.create_ask(
                    user_id=goal.user_id,
                    what=f"Unblock goal: {goal.title}",
                    why=(goal.last_failure_error or "repeated failures"),
                    blocks_goal_ids=[goal.id],
                )
        except Exception:
            logger.debug("blocked-goal ask creation skipped", exc_info=True)

    async def _self_wake(self, goal: Goal, session_id: str, final: str) -> None:
        """Forge a follow-up turn announcing the goal result (W1 rail). Fail-open."""
        try:
            deliver = getattr(self.task_agent, "deliver_self_wake", None)
            if deliver is None:
                return
            text = (f"Background goal '{goal.title}' completed.\n"
                    f"Result:\n{str(final)[:1500]}")
            delivered = await deliver(session_id, goal.user_id, text,
                                      metadata={"source": "goal", "goal_id": goal.id})
            if not delivered:
                # deliver_self_wake returns False when SELF_WAKE_ENABLED is off,
                # the session is remote/non-resident (dropped+audited), or the
                # reentry budget is exhausted -- the owner was NOT actually told,
                # so don't mark the episode surfaced (FIX1: that would make the
                # session-start digest silently omit this run).
                return
            # Task 7: the self-wake delivery IS "telling the owner" — mark this
            # goal's episode surfaced so the digest doesn't repeat it. Runs after
            # finalize_episode (already written earlier in _run_goal), so the row
            # exists for the UPDATE to find. Scoped to the goal's own user_id
            # (FIX2) since episodes are keyed on the composite (user_id,
            # session_id) and a bare session_id UPDATE could flip another
            # tenant's row on a collision.
            try:
                from modules.memory.registry import get_memory_registry
                prov = get_memory_registry().active()
                if prov is not None and hasattr(prov, "mark_episode_surfaced"):
                    prov.mark_episode_surfaced(session_id=session_id, user_id=goal.user_id)
            except Exception:
                logger.debug("goal self-wake surfaced-mark skipped for %s", goal.id,
                            exc_info=True)
        except Exception as e:
            logger.debug("goal self-wake skipped for %s: %s", goal.id, e)

    async def _maybe_plan(self, *, headroom_after: int) -> None:
        """Fire ONE planning session when the queue is thin. All gates mechanical."""
        from agents.task.constants import AutonomyConfig
        if not AutonomyConfig.goal_planner_enabled():
            return
        if headroom_after <= 0 and AutonomyConfig.goal_daily_quota() > 0:
            return
        min_ready = AutonomyConfig.goal_planner_min_ready()
        if len(self.board.ready(limit=min_ready)) >= min_ready:
            return
        # any tenant with an active objective (v1: single-user)
        users = {o.user_id for o in self._active_objective_owners()}
        if not users:
            return
        last = self.board.last_planner_run_at()
        cooldown = AutonomyConfig.goal_planner_cooldown_sec()
        import time as _time
        if last is not None and (_time.time() - last) < cooldown:
            return
        self.board.mark_planner_run()  # BEFORE dispatch: no double-fire while running
        user_id = sorted(users)[0]
        t = asyncio.create_task(self._run_planner(user_id))
        self._inflight.add(t)
        t.add_done_callback(self._inflight.discard)

    def _active_objective_owners(self):
        from agents.task.goals.board import OBJ_ACTIVE
        try:
            rows = self.board.list(status=OBJ_ACTIVE, limit=50)
            return [r for r in rows if r.kind == "objective"]
        except Exception:
            return []

    async def _run_planner(self, user_id: str) -> None:
        try:
            from agents.task.constants import AutonomyConfig
            from agents.task.goals.planner import (
                PLANNER_MAX_STEPS, PLANNER_TOOLS, build_planner_prompt,
            )
            deliverables_root = None
            try:
                from agents.task.path import pm
                if pm().is_project_root_workspace:
                    deliverables_root = pm().project_root
            except Exception:
                deliverables_root = None
            prompt = build_planner_prompt(
                self.board, user_id, deliverables_root,
                history_n=AutonomyConfig.goal_planner_history_n())
            from core.runtime_config import resolve_runtime_config
            provider = resolve_runtime_config(None, None)[0]
            model = None
            try:
                from modules.llm.llm_client_registry import get_default_model
                model = get_default_model(provider)
            except Exception:
                model = None
            request = {
                "task": prompt,
                "provider": provider,
                "model": model,
                "tools": list(PLANNER_TOOLS),
                "max_steps": PLANNER_MAX_STEPS,
                "temperature": 0.0,
            }
            session_id, final = await _run_task_as_session(
                self.task_agent, user_id=user_id, request=request, autonomous=True)
            logger.info("goal planner ran (session=%s): %s",
                        session_id, (final or "no result")[:200])
            await self._maybe_escalate_empty_pipeline(user_id, planner_summary=final)
        except Exception as e:
            logger.error("goal planner run failed: %s", e, exc_info=True)

    async def _maybe_escalate_empty_pipeline(self, user_id: str, *,
                                             planner_summary: Optional[str] = None) -> None:
        """§7.2 tail: a planner run that STILL leaves the board empty is a stall.

        After ``GOAL_EMPTY_PIPELINE_ESCALATE_AFTER`` consecutive such runs, surface
        the stall (with the planner's own last word — usually the concrete blocker)
        to the owner exactly once. Fail-open; both counters reset on refill.
        """
        try:
            if self.board.ready(limit=1):
                self._empty_planner_runs = 0
                self._empty_pipeline_escalated = False
                return
            self._empty_planner_runs += 1
            from agents.task.constants import AutonomyConfig
            if self._empty_planner_runs < AutonomyConfig.goal_empty_pipeline_escalate_after():
                return
            if self._empty_pipeline_escalated:
                return
            objective_title = None
            for o in self._active_objective_owners():
                if o.user_id == user_id:
                    objective_title = o.title
                    break
            from agents.task.goals.escalation import maybe_escalate_empty_pipeline
            sent = await maybe_escalate_empty_pipeline(
                self.task_agent, objective_title=objective_title,
                planner_summary=planner_summary)
            if sent:
                self._empty_pipeline_escalated = True
                # §7.2b: track the stall as an ask too, so it's fulfillable.
                try:
                    self.board.create_ask(
                        user_id=user_id,
                        what=f"Goal pipeline empty for '{objective_title or 'the objective'}'",
                        why=(planner_summary or "")[:2000],
                    )
                except Exception:
                    logger.debug("empty-pipeline ask creation skipped", exc_info=True)
        except Exception:
            logger.debug("empty-pipeline escalation skipped", exc_info=True)


class GoalTicker:
    """Periodically run dispatch_once until stopped (mirrors CronTicker)."""

    def __init__(self, dispatcher: GoalDispatcher, interval_seconds: int = 60):
        self.dispatcher = dispatcher
        self.interval_seconds = interval_seconds

    async def tick_once(self) -> int:
        return await self.dispatcher.dispatch_once()

    async def run_forever(self, stop_event: Optional[asyncio.Event] = None) -> None:
        from core.tickers import IntervalTicker
        from agents.task.constants import (
            ticker_idle_backoff_enabled,
            ticker_idle_backoff_max_multiplier,
        )

        is_active = None
        max_interval = None
        if ticker_idle_backoff_enabled():
            is_active = lambda dispatched: bool(dispatched)
            max_interval = self.interval_seconds * ticker_idle_backoff_max_multiplier()

        await IntervalTicker(
            self.dispatcher.dispatch_once,
            self.interval_seconds,
            is_active=is_active,
            max_interval_seconds=max_interval,
        ).run_forever(stop_event=stop_event)


def build_goal_ticker(task_agent: Any, *, data_dir: str = "data",
                      interval_seconds: Optional[int] = None) -> GoalTicker:
    """Assemble the goal stack into a GoalTicker the app lifespan can start/stop.

    Shares ``<data_dir>/goals.db`` with the agent-facing ``goal`` tool so goals the
    tool creates are picked up by the ticker; the tick lock keeps it safe under
    ``workers>1``.
    """
    from agents.task.constants import AutonomyConfig
    board = GoalBoard(os.path.join(data_dir, "goals.db"))
    dispatcher = GoalDispatcher(board, task_agent,
                               lock_path=os.path.join(data_dir, "goals.tick.lock"))
    return GoalTicker(dispatcher,
                      interval_seconds=interval_seconds or AutonomyConfig.goal_dispatch_interval_sec())
