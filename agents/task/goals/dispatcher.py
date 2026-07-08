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

from agents.task.goals.board import GoalBoard, Goal, STATUS_BLOCKED, STATUS_DONE, STATUS_READY
from agents.task.runtime.run_as_session import run_task_as_session as _run_task_as_session

logger = logging.getLogger(__name__)


def _goal_ev(goal, outcome: str, reason: Optional[str] = None, **extra) -> None:
    """T4-03: emit a goal_run event to the durable event log (fail-open).

    Goal runs previously wrote only the episodes table, never the event_log, so
    `polyrob telemetry` — the tool built to answer "what ran?" — showed cron and
    self-wake but not the goals that did most of the autonomous work. Mirror _cron_ev
    so goal lifecycle rides the same uniform autonomy/governance stream."""
    try:
        from agents.task.telemetry.event_log import get_event_log, event_log_enabled
        if event_log_enabled():
            get_event_log().record(
                "goal_run", user_id=getattr(goal, "user_id", ""),
                source="goal", goal_id=getattr(goal, "id", None),
                outcome=outcome, reason=reason, **extra)
    except Exception:
        pass


# Tools a self-decomposed CHILD goal may inherit from its parent when it set none.
# Deliberately excludes money/social/trading tools (wallet, x402, hyperliquid,
# polymarket, twitter) so the agent can't self-grant spend/post capability by
# spawning a child goal. Server-side allowlist, NOT agent-controllable.
CHILD_INHERITABLE_TOOLS = frozenset(
    {"filesystem", "task", "browser", "perplexity", "mcp", "anysite", "coding"}
)

# Safe default toolset when a goal sets no tools and has nothing inheritable.
_DEFAULT_GOAL_TOOLS = ["filesystem", "task"]

# WS-8 (compute posture): the compute tools an autonomous goal/cron run needs to
# actually build/run/serve. Provisioned ONLY at AGENT_COMPUTE_POSTURE>=1 (and each
# call still passes compute_posture_allows in-session — an owner-tenant autonomous
# run does). Sandbox-contained at posture 1; never money/social. Resolved at CALL
# time so posture 0 is byte-identical.
_COMPUTE_GOAL_TOOLS = ["code_execution", "shell", "coding"]


def _compute_posture_at_least_1() -> bool:
    try:
        from agents.task.constants import compute_posture
        return compute_posture() >= 1
    except Exception:
        return False


def default_goal_tools() -> list:
    """Posture-aware default goal toolset. Posture 0: ['filesystem','task']
    (byte-identical). Posture>=1: + the compute tools (code_execution/shell/coding)."""
    tools = list(_DEFAULT_GOAL_TOOLS)
    if _compute_posture_at_least_1():
        for t in _COMPUTE_GOAL_TOOLS:
            if t not in tools:
                tools.append(t)
    return tools


def child_inheritable_tools() -> frozenset:
    """Posture-aware child-inheritable set. Posture 0: the frozen module constant.
    Posture>=1: + code_execution/shell so a self-decomposed compute goal isn't
    tool-starved (still sandbox-contained; money/social remain excluded)."""
    if _compute_posture_at_least_1():
        return CHILD_INHERITABLE_TOOLS | {"code_execution", "shell"}
    return CHILD_INHERITABLE_TOOLS


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
        # Budget-gate push latch: tenants for whom an owner "over budget" push has
        # already gone out this over-budget episode. The durable ask is dedup-
        # refreshed every tick (fine), but the owner PUSH must fire ONCE per episode,
        # not every 60s tick per held goal. Cleared when the tenant is back under
        # budget (a fresh episode may push again). In-memory: resets on restart.
        self._budget_pushed: set = set()

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
            budget_aware = AutonomyConfig.budget_aware_autonomy()
            for g in ready:
                if budget_aware and await self._over_budget(g):
                    continue  # held — an owner-visible ask was raised instead of burning
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
        _goal_ev(goal, "started")
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
            # T2-01: a run that finished the loop but never called done() (max_steps
            # exhaustion, or a reply-only conversational exit) returns a non-refusal
            # status string that looks identical to a genuine completion. Recording it
            # as board success was the prod "marked done, never posted" failure. Inspect
            # the resident orchestrator's main-agent done signal; only a POSITIVE
            # "ran but no done()" (False) routes to the failure/escalation rail —
            # None (undeterminable) falls through to the legacy path unchanged. The
            # orchestrator is fetched once here and reused for provenance below.
            orchestrator = None
            try:
                orchestrator = self.task_agent.get_orchestrator(session_id)
            except Exception:
                orchestrator = None
            from agents.task.runtime.run_as_session import completed_via_done
            if completed_via_done(orchestrator) is False:
                await self._fail_run(
                    goal, session_id,
                    error="run ended without completing (no done() — likely ran out of steps)",
                    outcome=outcome)
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
                # Reuse the orchestrator fetched for the T2-01 done-check above.
                prov = await collect_provenance(orchestrator)
                await finalize_episode(
                    session_id=session_id, user_id=goal.user_id, kind="goal",
                    task=getattr(goal, "title", None), outcome="done", goal_id=goal.id,
                    summary=str(final)[:2000] if final is not None else None,
                    spend_usd=prov["spend_usd"], steps=prov["steps"],
                    artifacts=prov["artifacts"], meta={"source": "goal"},
                )
                recorded_success = True
                _goal_ev(goal, "done", session_id=session_id,
                         spend_usd=prov["spend_usd"], steps=prov["steps"])
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
            _goal_ev(goal, "failed", reason=str(e)[:200], session_id=session_id)
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
        # T4-03: surface the verified-failure outcome (blocked vs failed) in the durable
        # event log so `polyrob telemetry` reflects it, not just the episodes table.
        _goal_ev(goal, "blocked" if getattr(_g, "status", None) == STATUS_BLOCKED else "failed",
                 reason=str(error)[:200], session_id=session_id)
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
                inheritable = child_inheritable_tools()  # posture-aware (WS-8)
                inherited = [t for t in parent_tools if t in inheritable]
                if inherited:
                    return inherited
        return default_goal_tools()  # posture-aware (WS-8)

    async def _over_budget(self, goal) -> bool:
        """True when the goal's tenant has spent >= the trailing-window budget.

        Consults the unified ledger per-goal (the ledger is per-tenant; the ready
        set is cross-tenant). Over budget -> raise a durable owner-visible ask and
        optionally push, then hold the goal (left 'ready'). Fail-open: any ledger
        error returns False so a metering hiccup never stalls dispatch."""
        try:
            from agents.task.constants import AutonomyConfig
            budget = AutonomyConfig.autonomy_budget_usd()
            if budget <= 0:
                return False
            window = AutonomyConfig.autonomy_budget_window_days()
            from modules.credits.unified_ledger import build_ledger
            ledger = await build_ledger(goal.user_id, days=window)
            spent = float(ledger.get("total_spend_usd", 0.0) or 0.0)
            if spent < budget:
                # Back under budget — clear the push latch so a future over-budget
                # episode for this tenant escalates again.
                self._budget_pushed.discard(goal.user_id)
                return False
        except Exception:
            logger.debug("budget gate check failed (fail-open)", exc_info=True)
            return False
        # Over budget — raise a durable, dedup-refreshing ask (owner-visible) and
        # optionally push. Never claim/run the goal.
        try:
            self.board.create_ask(
                user_id=goal.user_id,
                what="Autonomous goals paused — spend budget reached",
                why=(f"Spent ${spent:.2f} of ${budget:.2f} over the last {window}d; "
                     f"goal '{goal.title}' held. Raise AUTONOMY_BUDGET_USD or fulfill "
                     "this ask to resume."),
                blocks_goal_ids=[goal.id],
            )
        except Exception:
            logger.debug("budget ask creation skipped", exc_info=True)
        # Owner PUSH once per over-budget episode (the ask row above is the durable,
        # dedup-refreshed record; the push must not spam every tick per held goal).
        if goal.user_id not in self._budget_pushed:
            try:
                from core.self_evolution import push_owner_message
                container = getattr(self.task_agent, "container", None)
                if container is not None:
                    await push_owner_message(
                        container,
                        f"[budget] ${spent:.2f}/${budget:.2f} spent over {window}d — autonomous "
                        f"goals paused. Goal '{goal.title}' held.")
                self._budget_pushed.add(goal.user_id)
            except Exception:
                logger.debug("budget push skipped", exc_info=True)
        logger.info("goal %s held: tenant over budget ($%.2f >= $%.2f)",
                    goal.id, spent, budget)
        return True

    async def _maybe_escalate_blocked(self, goal) -> None:
        """§7.2: when record_failure tripped the breaker (goal now 'blocked'), surface
        a concrete ask to the owner instead of letting it die silently. Fail-open."""
        try:
            from agents.task.goals.escalation import maybe_escalate_blocked
            await maybe_escalate_blocked(self.task_agent, goal)
        except Exception:
            logger.debug("blocker escalation skipped", exc_info=True)
        # §7.2b / T2-03 / T4-04: ALWAYS leave a TRACKED ask for a blocked goal so the
        # need survives even when no owner push went out. This ask creation was gated on
        # the SAME goal_blocker_escalation() flag as the push, so with the default OFF a
        # blocked goal left NO ask and `owner asks/fulfill` had nothing to consume — the
        # need evaporated silently (the prod "X-write gap never escalated" shape). The
        # ask is silent + durable + tenant-scoped and create_ask dedup-refreshes, so it
        # is safe unconditionally; only the PUSH above stays posture-gated.
        try:
            from agents.task.goals.board import STATUS_BLOCKED
            if getattr(goal, "status", None) == STATUS_BLOCKED:
                self.board.create_ask(
                    user_id=goal.user_id,
                    what=f"Unblock goal: {goal.title}",
                    why=(goal.last_failure_error or "repeated failures"),
                    blocks_goal_ids=[goal.id],
                )
        except Exception:
            logger.debug("blocked-goal ask creation skipped", exc_info=True)

    async def _self_wake(self, goal: Goal, session_id: str, final: str) -> None:
        """Surface a completed goal's result (W1 rail). Fail-open.

        T2-02: ``deliver_self_wake`` re-enters the goal's OWN just-finished session — an
        empty room the owner never watches — so a completed goal told no one. In
        addition to the (best-effort, agent-continuation) self-wake, PUSH the result to
        the OWNER via ``push_owner_message`` (durable: a sink-less local owner still sees
        it via ``polyrob telemetry``, per T4-04), so the owner is reliably told
        regardless of whether a live session is watching. (Retargeting the
        agent-continuation wake at the owner's live chat session needs a
        latest-session-for-user resolver on the chat registry — deferred.)
        """
        try:
            text = (f"✅ Background goal '{goal.title}' completed.\n"
                    f"Result:\n{str(final)[:1500]}")
            # T2-02: tell the OWNER (surface-independent, durable).
            owner_told = False
            try:
                from core.self_evolution import push_owner_message
                owner_told = await push_owner_message(
                    getattr(self.task_agent, "container", None), text)
            except Exception:
                logger.debug("goal completion owner-push skipped for %s", goal.id,
                             exc_info=True)
            # Best-effort agent-continuation via the existing self-wake rail.
            deliver = getattr(self.task_agent, "deliver_self_wake", None)
            delivered = False
            if deliver is not None:
                delivered = await deliver(session_id, goal.user_id, text,
                                          metadata={"source": "goal", "goal_id": goal.id})
            if not (delivered or owner_told):
                # Nobody was actually told (self-wake off/dropped/budget-exhausted AND no
                # owner push landed) -- don't mark the episode surfaced (FIX1: that would
                # make the session-start digest silently omit this run).
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
            # T2-03/T4-04: mark the stall escalated once the threshold is reached,
            # independent of whether the owner PUSH lands — the durable ask below is the
            # owner-visible artifact and must be created even under the silent posture.
            # (_empty_pipeline_escalated resets when the board refills, so this stays
            # once-per-stall and never spams.)
            self._empty_pipeline_escalated = True
            from agents.task.goals.escalation import maybe_escalate_empty_pipeline
            await maybe_escalate_empty_pipeline(
                self.task_agent, objective_title=objective_title,
                planner_summary=planner_summary)
            # §7.2b: track the stall as an ask so it is fulfillable regardless of push.
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
