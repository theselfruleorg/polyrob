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
from pathlib import Path
from typing import Any, Optional

from agents.task.goals.board import GoalBoard, Goal, STATUS_BLOCKED, STATUS_DONE, STATUS_READY
from agents.task.runtime.run_as_session import (
    run_task_as_session as _run_task_as_session,  # noqa: F401 — legacy seam, kept for planner
    run_task_to_outcome as _run_task_to_outcome,
)

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


# 015 #3: greppable marker for "this run failed because the LLM/provider is
# down" (billing/402/exhausted-fallback family) — written as a prefix into
# goals.last_failure_error so a provider outage is distinguishable from
# "run did not complete (refusal or empty)" without grepping journald.
LLM_EXHAUSTED_MARKER = "llm_provider_exhausted"

# Provider-exhaustion phrasings the credit-death markers don't cover: the
# LLMProviderExhaustedError halt strings ("ALL LLM PROVIDERS EXHAUSTED" /
# "All LLM providers failed. Tried: [...]") carry no 402/billing text.
_PROVIDER_EXHAUSTED_TEXT_MARKERS = ("all llm providers", "providers exhausted")


def _is_llm_provider_exhausted(err: Any) -> bool:
    """015 #3: classify a goal-run failure as a permanent LLM/provider error.

    Accepts an exception (walks the ``__cause__``/``__context__`` chain for the
    ``LLMPermanentError`` family) or a status string (the refusal path's
    "Session failed: PERMANENT ERROR: … 402 …"). The string check reuses the
    credit-death classifier the sentinel already owns
    (``core.credit_sentinel.looks_like_credit_death`` — 402/quota/billing
    markers) plus the ALL-PROVIDERS-EXHAUSTED phrasing, which carries no
    billing text. Fail-open to False — a broken classifier must never change
    what gets recorded, only how it is labeled.
    """
    try:
        from core.credit_sentinel import looks_like_credit_death

        def _text_matches(text: str) -> bool:
            low = text.lower()
            return looks_like_credit_death(low) or any(
                m in low for m in _PROVIDER_EXHAUSTED_TEXT_MARKERS)

        if isinstance(err, BaseException):
            try:
                from core.exceptions import LLMPermanentError, LLMProviderExhaustedError
                permanent: tuple = (LLMPermanentError, LLMProviderExhaustedError)
            except Exception:
                permanent = ()
            seen: set = set()
            e: Optional[BaseException] = err
            while e is not None and id(e) not in seen:
                seen.add(id(e))
                if permanent and isinstance(e, permanent):
                    return True
                if _text_matches(str(e)):
                    return True
                e = e.__cause__ or e.__context__
            return False
        return bool(err) and _text_matches(str(err))
    except Exception:
        return False


# Tools a self-decomposed CHILD goal may inherit from its parent when it set none.
# Deliberately excludes money/social/trading tools (wallet, x402, hyperliquid,
# polymarket, twitter) so the agent can't self-grant spend/post capability by
# spawning a child goal. Server-side allowlist, NOT agent-controllable.
CHILD_INHERITABLE_TOOLS = frozenset(
    {"filesystem", "task", "browser", "perplexity", "mcp", "anysite", "coding"}
)

# Safe default toolset when a goal sets no tools and has nothing inheritable (SSOT).
from agents.task.constants import BASE_DEFAULT_TOOLS as _BASE_DEFAULT_TOOLS
_DEFAULT_GOAL_TOOLS = list(_BASE_DEFAULT_TOOLS)

# WS-8 (compute posture): the compute tools an autonomous goal/cron run needs to
# actually build/run/serve are provisioned ONLY at AGENT_COMPUTE_POSTURE>=1 (and
# each call still passes compute_posture_allows in-session). The list itself is
# the SSOT in agents.task.tool_defaults.with_compute_tools (014 A2).


def _compute_posture_at_least_1() -> bool:
    try:
        from agents.task.constants import compute_posture
        return compute_posture() >= 1
    except Exception:
        return False


def _compute_posture_at_least_2() -> bool:
    try:
        from agents.task.constants import compute_posture
        return compute_posture() >= 2
    except Exception:
        return False


def _hf_deploy_goal_tool_enabled() -> bool:
    """hf_deploy is a self-maintenance-tier (posture>=2) capability AND stays
    gated on its own flag — attaching an unregistered tool_id to a goal's
    toolset would be a dead entry when HF_DEPLOY_ENABLED is off."""
    if not _compute_posture_at_least_2():
        return False
    try:
        from tools.hf_deploy import hf_deploy_enabled
        return hf_deploy_enabled()
    except Exception:
        return False


def default_goal_tools() -> list:
    """Posture-aware, mode-aware default goal toolset. Supervised (default):
    posture 0 = ['filesystem','task'] (byte-identical); posture>=1: + the compute
    tools (code_execution/shell/coding); posture>=2 (+HF_DEPLOY_ENABLED): + hf_deploy
    (self-maintenance tier). Under effective AUTONOMY_MODE=autonomous the base
    switches to the full AUTONOMOUS_MODE_TOOLS grant (never money/host — those
    still ride compute posture, unaffected by mode)."""
    from agents.task.constants import full_autonomy_enabled, AUTONOMOUS_MODE_TOOLS
    from agents.task.tool_defaults import with_compute_tools
    tools = list(AUTONOMOUS_MODE_TOOLS) if full_autonomy_enabled() else list(_DEFAULT_GOAL_TOOLS)
    with_compute_tools(tools)  # SSOT for the posture>=1 additions (014 A2)
    if _hf_deploy_goal_tool_enabled() and "hf_deploy" not in tools:
        tools.append("hf_deploy")
    return tools


def child_inheritable_tools() -> frozenset:
    """Posture-aware child-inheritable set. Posture 0: the frozen module constant.
    Posture>=1: + code_execution/shell so a self-decomposed compute goal isn't
    tool-starved (still sandbox-contained; money/social remain excluded)."""
    if _compute_posture_at_least_1():
        return CHILD_INHERITABLE_TOOLS | {"code_execution", "shell"}
    return CHILD_INHERITABLE_TOOLS


def effective_goal_max_concurrent(user_id: Optional[str], home_dir) -> int:
    """Owner's in-flight goal cap: pref (min-merged, spec ``goals.max_concurrent``)
    over the ``GOAL_MAX_CONCURRENT`` env default. No pref file present =>
    byte-identical to ``AutonomyConfig.goal_max_concurrent()`` (owner-UX P1 T4).

    Sentinel fix (owner-UX P1 final review): ``<=0`` is the env accessor's
    "disabled" sentinel, NOT a real ceiling of 0 — feeding it straight into the
    min-merge let ``env<=0`` silently win over ANY pref (``min(pref, 0) == 0``),
    so an owner pref could never actually tighten an unset/disabled env cap.
    Mirrors ``core.wallet.config.effective_daily_cap_usd``: a non-positive raw
    value is passed to the resolver as ``env_value=None`` (no floor), so a pref
    ALONE can still set a cap; with no pref file present this still returns the
    raw (possibly ``<=0``) legacy value unchanged."""
    from agents.task.constants import AutonomyConfig
    from core import prefs
    raw = AutonomyConfig.goal_max_concurrent()
    env_value = raw if raw > 0 else None
    out = prefs.resolve("goals.max_concurrent", user_id, home_dir,
                        env_value=env_value, default=None)
    return out if out is not None else raw


def effective_goal_quota(user_id: Optional[str], home_dir) -> int:
    """Owner's per-day autonomous-goal-run cap: pref (min-merged, spec
    ``goals.daily_quota``) over the ``GOAL_DAILY_QUOTA`` env default. No pref
    file present => byte-identical to ``AutonomyConfig.goal_daily_quota()``
    (owner-UX P1 T4).

    Sentinel fix (owner-UX P1 final review): ``GOAL_DAILY_QUOTA<=0`` means
    "disabled" (unlimited runs/day) per ``AutonomyConfig.goal_daily_quota()``'s
    own docstring — NOT a ceiling of 0, so it must not be fed into the
    min-merge as a real floor (env=0 + pref=5 previously resolved to
    ``min(5, 0) == 0``, which callers then read as "unlimited", silently
    discarding the owner's requested cap of 5). See
    ``core.wallet.config.effective_daily_cap_usd`` for the same pattern."""
    from agents.task.constants import AutonomyConfig
    from core import prefs
    raw = AutonomyConfig.goal_daily_quota()
    env_value = raw if raw > 0 else None
    out = prefs.resolve("goals.daily_quota", user_id, home_dir,
                        env_value=env_value, default=None)
    return out if out is not None else raw


def effective_goal_notify_on_done(user_id: Optional[str], home_dir) -> bool:
    """Owner's goal-completion push switch: pref (override, spec
    ``goals.notify_on_done``) over the ``GOAL_NOTIFY_ON_DONE`` env default.
    No pref file present => byte-identical to
    ``AutonomyConfig.goal_notify_on_done()`` (018 P0.2 — this key was DEAD:
    settable/displayed but the run-completion path read the env directly)."""
    from agents.task.constants import AutonomyConfig
    from core import prefs
    env_value = AutonomyConfig.goal_notify_on_done()
    return bool(prefs.resolve("goals.notify_on_done", user_id, home_dir,
                              env_value=env_value, default=env_value))


def _tick_owner_user_id() -> Optional[str]:
    """Representative tenant for TICK-level (pre-claim, cross-tenant) autonomy
    knobs — the in-flight cap and daily quota are read ONCE per dispatch tick
    (design constraint: resolve once per tick, not per item), before the ready
    set (which may span tenants) is even fetched. v1 is single-owner (mirrors
    ``_maybe_plan``'s ``sorted(users)[0]`` convention elsewhere in this file),
    so the resolved owner principal is the sound representative tenant.
    Fail-open to None (=> legacy env-only value; see ``core.prefs.resolve``)."""
    try:
        from core.instance import resolve_owner_principal
        return resolve_owner_principal()
    except Exception:
        return None


def _deliverables_root() -> Optional[Path]:
    """The shared-workspace root, or None — one resolution shared by the planner's
    EXISTING DELIVERABLES listing AND the T9 artifact-existence stamping (goal task
    + planner BLOCKED list). Per-session server workspaces have no single root pm()
    can name (each session is its own sandbox), so this is scoped to the shared
    project-root workspace (CLI/headless project mode) exactly like
    ``effective_goal_concurrency``'s clamp above. Fail-open to None."""
    try:
        from agents.task.path import pm
        if pm().is_project_root_workspace:
            return pm().project_root
    except Exception:
        pass
    return None


def effective_goal_concurrency(user_id: Optional[str] = None, home_dir=None) -> int:
    """Goal in-flight cap, clamped to single-flight on a SHARED project folder.

    When the installed pm() serves one project-root workspace (CLI/headless project
    mode), concurrent goal runs would interleave read-modify-write edits on the same
    files (the battle-test "read INDEX.md, append" corruption). Serialize them.
    Keyed off the installed pm() — NOT an env var — so the multi-tenant server (whose
    global pm() is per-session) keeps its full GOAL_MAX_CONCURRENT (MT-5). Fail-open
    to the unclamped cap on any pm() error.

    ``user_id``/``home_dir`` (owner-UX P1 T4, both optional, default None) thread
    an owner preference (spec ``goals.max_concurrent``) over the env cap via
    :func:`effective_goal_max_concurrent`; the legacy zero-arg call resolves to
    the env-only value unchanged (``user_id=None`` => no pref file can match).
    """
    cap = effective_goal_max_concurrent(user_id, home_dir)
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

    def _home_dir(self) -> str:
        """Data-home for pref resolution (owner-UX P1 T4), derived from the
        board's OWN db path — reuses the data_dir the ticker/board were built
        with (``build_goal_ticker(data_dir=...)``) rather than inventing a new
        global default. Fail-open to "data" (the same default `data_dir` takes)."""
        try:
            import os
            from core.runtime_paths import data_dir_or_home
            return data_dir_or_home(os.path.dirname(self.board.db_path))
        except Exception:
            return data_dir_or_home(None)

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
        # §5.3: age ancient blocked goals out VISIBLY (-> cancelled, logged)
        # instead of letting them rot as permanent planner context.
        try:
            max_age = AutonomyConfig.goal_blocked_max_age_days()
            if max_age > 0:
                aged = self.board.age_out_blocked(max_age_days=max_age)
                if aged:
                    logger.info("goal dispatch: aged out %d ancient blocked goal(s)", aged)
        except Exception:
            logger.debug("blocked-goal aging skipped", exc_info=True)

        if not AutonomyConfig.goals_enabled():
            return 0

        # Owner kill-switch: halt ALL autonomous dispatch (togglable without restart).
        if AutonomyConfig.autonomy_halted():
            if not getattr(self, "_halt_logged", False):
                logger.warning("goal dispatch HALTED (AUTONOMY_HALT / halt-file) — no autonomous runs")
                self._halt_logged = True
            return 0
        self._halt_logged = False

        # §6.3 provider-credit sentinel: while tripped (recent 402/credit-death),
        # burning more paid runs is pointless — pause dispatch until auto-release.
        try:
            from core.credit_sentinel import credit_sentinel_active
            if credit_sentinel_active():
                if not getattr(self, "_sentinel_logged", False):
                    logger.warning("goal dispatch paused: provider-credit sentinel active")
                    self._sentinel_logged = True
                return 0
            self._sentinel_logged = False
        except Exception:
            pass

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
            # owner-UX P1 T4: resolve the tick's home_dir/representative-tenant ONCE
            # (design constraint: not per ready-goal item) so the in-flight cap and
            # daily quota below can respect an owner preference that tightens them.
            _home_dir = self._home_dir()
            _owner_uid = _tick_owner_user_id()
            limit = effective_goal_concurrency(_owner_uid, _home_dir)
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
            quota = effective_goal_quota(_owner_uid, _home_dir)
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
                    # §5.4: quota exhaustion pauses RUNS, not curation — the
                    # planner may still top up the board (its own cooldown +
                    # min-ready gates bound the cost); queued goals run when
                    # the quota window rolls over.
                    try:
                        await self._maybe_plan(headroom_after=0)
                    except Exception:
                        logger.debug("quota-paused planning skipped", exc_info=True)
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
        # 012 #1: keep the run envelope reachable from the outer exception handler
        # so a failure AFTER real work still records honest steps/spend/artifacts.
        run = None
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
        # 019 P2: tell the owner an autonomous run STARTED (not just completed/
        # digest). Rides the one delivery rail (dedup + caps); posture-gated
        # default (ON under full/autonomous), fail-open.
        if AutonomyConfig.autonomy_start_notice():
            try:
                from core.self_evolution import push_owner_message
                _title = (goal.title or goal.body or "")[:120]
                await push_owner_message(
                    getattr(self.task_agent, "container", None),
                    f"▶ goal started: {_title} ({goal.id[:8]})")
            except Exception:
                logger.debug("goal start notice failed for %s", goal.id, exc_info=True)
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
                "task": build_goal_run_task(goal, objective, workspace_root=_deliverables_root()),
                "provider": provider,
                "model": model,
                "tools": self._resolve_tools(goal),
                "max_steps": payload.get("max_steps", 20),
                "temperature": 0.0,
                "goal_id": goal.id,
            }
            # §6.2 fail-closed: a money-enabled run must not START unmetered —
            # without a database_manager, spend tracking is blind on a live
            # wallet. Clear recorded error, never a silent spend loop.
            from agents.task.runtime.metering_gate import unmetered_money_gate
            _gate_err = unmetered_money_gate(self.task_agent, request.get("tools"))
            if _gate_err is not None:
                _g = self.board.record_failure(goal.id, error=_gate_err)
                await self._maybe_escalate_blocked(_g)
                return
            # Route through the shared helper: create_session → run_session → RunOutcome.
            # H11: hard wall-clock cap (mirrors cron's per-job wait_for). max_steps alone
            # doesn't bound wall time — a single hung step would occupy a slot forever. On
            # timeout the TimeoutError is handled by the except below (record_failure) and
            # the finally cancels the claim heartbeat, so reclaim_stale can recover the slot.
            _max_run = AutonomyConfig.goal_max_run_seconds()
            run = await asyncio.wait_for(
                _run_task_to_outcome(
                    self.task_agent, user_id=goal.user_id, request=request, autonomous=True
                ),
                timeout=_max_run,
            )
            session_id = run.session_id
            if session_id is None:
                _g = self.board.record_failure(goal.id, error="create_session returned no id")
                await self._maybe_escalate_blocked(_g)
                return
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
                if _is_llm_provider_exhausted(run.status):
                    error = f"{LLM_EXHAUSTED_MARKER}: {str(run.status)[:400]}"
                _g = self.board.record_failure(
                    goal.id, error=error, session_id=session_id)
                await self._maybe_escalate_blocked(_g)
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
                return
            # §2: every downstream read comes from the envelope — the done() ledger
            # text, the BLOCKED declaration, provenance — never from re-extracted
            # message-history strings (the goal-58a1385d18bf corruption).
            final = run.result_text()
            outcome = run.outcome_line
            # §3.1: an agent-declared 'OUTCOME: BLOCKED — <need>' is an honest
            # failure exit, never a success. Checked BEFORE record_success so the
            # goal routes through the breaker/escalation rail with its stated need.
            if run.blocked:
                await self._fail_blocked_declared(
                    goal, session_id, outcome, run.blocked_need,
                    agent_reported=bool(run.user_messages),
                    steps=run.steps, spend_usd=run.spend_usd,
                    artifacts=run.artifacts)
                return
            # T2-01: a run that finished the loop but never called done() (max_steps
            # exhaustion, or a reply-only conversational exit) returns a non-refusal
            # status string that looks identical to a genuine completion. Recording it
            # as board success was the prod "marked done, never posted" failure. Only a
            # POSITIVE "ran but no done()" (False) routes to the failure/escalation
            # rail — None (undeterminable) falls through to the legacy path unchanged.
            if run.done_called is False:
                await self._fail_run(
                    goal, session_id,
                    error="run ended without completing (no done() — likely ran out of steps)",
                    outcome=outcome,
                    steps=run.steps, spend_usd=run.spend_usd,
                    artifacts=run.artifacts)
                return
            # §4.2 NEW invariant: a done() where EVERY substantive action errored
            # is not a judgment call — nothing executed successfully, so the claim
            # has no basis. Deterministic, needs no goal semantics.
            if run.all_actions_errored:
                await self._fail_run(
                    goal, session_id,
                    error="done() after every action errored — nothing executed successfully",
                    outcome=outcome,
                    steps=run.steps, spend_usd=run.spend_usd,
                    artifacts=run.artifacts)
                return
            # §4.4: typed acceptance checks — optional sharpener. When a producer
            # set them they run fail-CLOSED and their results join the evidence
            # pack; nothing rejects a goal without them.
            checks = payload.get("acceptance_checks") or []
            if checks:
                from agents.task.runtime.acceptance_checks import (
                    run_acceptance_checks, failed_checks)
                _ws = None
                try:
                    from agents.task.runtime.evidence import _resolve_workspace_dir
                    _ws = _resolve_workspace_dir(self.task_agent.get_orchestrator(session_id))
                except Exception:
                    _ws = None
                check_results = await run_acceptance_checks(checks, workspace_dir=_ws)
                if run.evidence is not None:
                    try:
                        run.evidence.checks = check_results
                    except Exception:
                        pass
                _failed = failed_checks(check_results)
                if _failed:
                    await self._fail_run(
                        goal, session_id,
                        error=("acceptance checks failed: " + "; ".join(
                            f"{c.get('type')}: {c.get('detail')}" for c in _failed))[:2000],
                        outcome=outcome,
                        steps=run.steps, spend_usd=run.spend_usd,
                        artifacts=run.artifacts)
                    return
            # §4.3 evidence-grounded completion review (autonomous runs): the
            # CLAIM read against the evidence pack — no acceptance required.
            # unmet (claim contradicted by evidence) -> failure with the gap;
            # met -> verified; unclear/error/timeout -> done (UNVERIFIED) — the
            # run completes (a framework for arbitrary goals must not hard-block
            # fuzzy work) but the label is honest everywhere and learning loops
            # do not consume it.
            import agents.task.goals.completion_judge as _cj
            judge_on = AutonomyConfig.goal_completion_judge()
            if judge_on:
                verdict, reason = await _cj.judge_run_outcome(
                    self.task_agent, session_id, goal, run)
                if verdict == _cj.VERDICT_UNMET:
                    await self._fail_run(
                        goal, session_id,
                        error=f"completion judge: {reason} (claim contradicted by evidence)"[:2000],
                        outcome=outcome,
                        steps=run.steps, spend_usd=run.spend_usd,
                        artifacts=run.artifacts)
                    return
                run.verified = "verified" if verdict == _cj.VERDICT_MET else "unverified"
            # Honest result recording: the envelope filters placeholders/status
            # strings, so an empty result_text means the run genuinely produced no
            # recoverable text — say THAT instead of shipping a placeholder.
            result_record = final or "(completed via done() — no textual output captured)"
            self.board.record_success(goal.id, session_id=session_id, result=result_record[:4000])
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
                recorded_success = True
                _goal_ev(goal, "done", session_id=session_id,
                         spend_usd=run.spend_usd, steps=run.steps,
                         artifacts=len(run.artifacts),
                         user_messages=len(run.user_messages),
                         verified=run.verified)
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
            if effective_goal_notify_on_done(goal.user_id, self._home_dir()) \
                    and not run.user_messages:
                await self._notify_owner_done(goal, session_id, result_record,
                                              verified=run.verified if judge_on else "verified")
            # §4.3: an UNVERIFIED completion earns nothing downstream — no
            # self-wake re-entry. With the judge disabled, legacy behavior holds.
            if AutonomyConfig.goal_self_wake_enabled() and \
                    (not judge_on or run.verified == "verified"):
                await self._self_wake(goal, session_id, result_record)
        except Exception as e:
            logger.error("goal %s run failed: %s", goal.id, e, exc_info=True)
            # 015 #3: a permanent LLM/provider death (OpenRouter 402 →
            # LLMPermanentError → "ALL LLM PROVIDERS EXHAUSTED") gets the
            # distinct greppable marker in goals.last_failure_error.
            error_text = str(e)
            if _is_llm_provider_exhausted(e):
                error_text = f"{LLM_EXHAUSTED_MARKER}: {error_text}"[:2000]
            _goal_ev(goal, "failed", reason=error_text[:200], session_id=session_id)
            try:
                _g = self.board.record_failure(goal.id, error=error_text, session_id=session_id)
                await self._maybe_escalate_blocked(_g)
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
        finally:
            hb_task.cancel()
            if _shared_ws:
                from core.interactive_gate import mark_idle
                mark_idle()

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
        await self._maybe_escalate_blocked(_g, agent_reported=agent_reported)
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
        base = default_goal_tools()  # posture-aware (WS-8)
        # Proposal 009 (2026-07-14): a goal with no tools payload whose own text names a
        # capability ("Publish ... X thread" → twitter) resolves it at dispatch time instead
        # of starving — the night-1 battle-test failure mode for legacy/self-created rows.
        # Same allowlisted inference goal_create applies at create time; a goal whose text
        # names no known tool resolves byte-identically to the plain default.
        try:
            from tools.goal_tools import _infer_tools_from_text
            inferred = _infer_tools_from_text(getattr(goal, "title", None),
                                              getattr(goal, "body", None),
                                              payload.get("acceptance"))
        except Exception:
            inferred = set()
        if inferred:
            return sorted(set(base) | inferred)
        return base

    async def _maybe_escalate_blocked(self, goal, *, agent_reported: bool = False) -> None:
        """§7.2: when record_failure tripped the breaker (goal now 'blocked'), surface
        a concrete ask to the owner instead of letting it die silently. Fail-open.

        §3.4: the escalation PUSH is a safety net — when the agent itself already
        reported the block to its user during the run (``agent_reported``, from
        RunOutcome.user_messages), the push is skipped; the durable ask below is
        ALWAYS left either way."""
        if not agent_reported:
            try:
                from agents.task.goals import escalation as _escalation
                await _escalation.maybe_escalate_blocked(self.task_agent, goal)
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

    def _completion_text(self, goal: Goal, final: str, verified: str = "verified") -> str:
        # §4.3: the ✅ is EARNED — an unverified completion is labeled honestly,
        # never pushed as a green checkmark on an unchecked claim.
        if verified == "verified":
            head = f"✅ Background goal '{goal.title}' completed."
        else:
            head = f"Background goal '{goal.title}' finished — done (unverified)."
        return f"{head}\nResult:\n{str(final)[:1500]}"

    def _mark_episode_surfaced(self, goal: Goal, session_id: str) -> None:
        """Mark this goal's episode surfaced so the session-start digest doesn't repeat
        it. Scoped to the goal's own user_id (FIX2 — episodes key on the composite
        (user_id, session_id); a bare session_id UPDATE could flip another tenant's row
        on a collision). Fail-open."""
        try:
            from modules.memory.registry import get_memory_registry
            prov = get_memory_registry().active()
            if prov is not None and hasattr(prov, "mark_episode_surfaced"):
                prov.mark_episode_surfaced(session_id=session_id, user_id=goal.user_id)
        except Exception:
            logger.debug("goal surfaced-mark skipped for %s", goal.id, exc_info=True)

    async def _notify_owner_done(self, goal: Goal, session_id: str, final: str,
                                 verified: str = "verified") -> bool:
        """Tell the OWNER a background goal COMPLETED — surface-independent + durable.

        This is the completion-communication rail, DECOUPLED from the self-wake
        agent-re-entry (which is posture-gated OFF on the server). The push used to
        live only inside ``_self_wake``, so with self-wake off, completed goals told
        no one — the observed "goals never report completions" gap. ``push_owner_message``
        delivers to the owner's Telegram if a sink+chat exist, else persists a durable
        ``owner_notice`` (visible via ``polyrob telemetry`` / the digest, per T4-04), so
        the owner is reliably informed either way. Fail-open. Returns whether told."""
        owner_told = False
        try:
            from core.self_evolution import push_owner_message
            owner_told = await push_owner_message(
                getattr(self.task_agent, "container", None),
                self._completion_text(goal, final, verified=verified))
        except Exception:
            logger.debug("goal completion owner-push skipped for %s", goal.id, exc_info=True)
        if owner_told:
            self._mark_episode_surfaced(goal, session_id)
        return owner_told

    async def _self_wake(self, goal: Goal, session_id: str, final: str) -> None:
        """Agent-continuation: re-enter the goal's OWN just-finished session as a forged
        turn (W1 rail), so the agent can act on its own completion. OWNER notification is
        handled separately by ``_notify_owner_done`` (always-on). Fail-open.
        (Retargeting the wake at the owner's live chat session needs a
        latest-session-for-user resolver on the chat registry — deferred.)"""
        try:
            deliver = getattr(self.task_agent, "deliver_self_wake", None)
            if deliver is None:
                return
            delivered = await deliver(session_id, goal.user_id,
                                      self._completion_text(goal, final),
                                      metadata={"source": "goal", "goal_id": goal.id})
            if delivered:
                self._mark_episode_surfaced(goal, session_id)
        except Exception as e:
            logger.debug("goal self-wake skipped for %s: %s", goal.id, e)

    async def _maybe_plan(self, *, headroom_after: int) -> None:
        """Fire ONE planning session when the queue is thin. All gates mechanical."""
        from agents.task.constants import AutonomyConfig
        if not AutonomyConfig.goal_planner_enabled():
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
                planner_session_tools,
            )
            deliverables_root = _deliverables_root()
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
                "tools": planner_session_tools(),
                "max_steps": PLANNER_MAX_STEPS,
                "temperature": 0.0,
            }
            session_id, final = await _run_task_as_session(
                self.task_agent, user_id=user_id, request=request, autonomous=True)
            # 015 #3 (planner leg): a planner run killed by provider exhaustion
            # must not read as "planner correctly found nothing to do" — that
            # ambiguity hid a 13h board-dark outage from two intel reviews.
            from core.credit_sentinel import credit_sentinel_active
            if _is_llm_provider_exhausted(final or "") or \
                    (not final and credit_sentinel_active()):
                logger.error(
                    "%s: goal planner run died on provider outage, NOT an "
                    "empty pipeline (session=%s): %s",
                    LLM_EXHAUSTED_MARKER, session_id,
                    (final or "no output")[:200])
                return
            logger.info("goal planner ran (session=%s): %s",
                        session_id, (final or "no result")[:200])
            await self._maybe_escalate_empty_pipeline(user_id, planner_summary=final)
        except Exception as e:
            if _is_llm_provider_exhausted(e):
                logger.error("%s: goal planner run failed on provider outage: %s",
                             LLM_EXHAUSTED_MARKER, e)
            else:
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
