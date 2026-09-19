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
import json
import os
from pathlib import Path
from typing import Any, List, Optional

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
        from core.event_log import get_event_log, event_log_enabled
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

# T1.1 validation fix (2026-07-23): the RUN_BUDGET_USD halt marker, greppable
# in goals.last_failure_error just like LLM_EXHAUSTED_MARKER — a budget-halted
# goal was previously indistinguishable from any other refusal on the board.
try:
    from agents.task.agent.core.run_budget import RUN_BUDGET_MARKER
except ImportError:  # pragma: no cover - core-only install
    RUN_BUDGET_MARKER = "run_budget_exhausted"

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
    from agents.task.constants import effective_autonomous_tools, full_autonomy_enabled
    from agents.task.tool_defaults import with_compute_tools
    # `effective_autonomous_tools()` (056 WS4) = `autonomous_mode_tools()` — which
    # folds in the defi rail when DEFI_AGENT_AUTONOMY is armed — minus tools this
    # deploy cannot serve right now (email on rejected SMTP credentials).
    tools = list(effective_autonomous_tools()) if full_autonomy_enabled() else list(_DEFAULT_GOAL_TOOLS)
    with_compute_tools(tools)  # SSOT for the posture>=1 additions (014 A2)
    if _hf_deploy_goal_tool_enabled() and "hf_deploy" not in tools:
        tools.append("hf_deploy")
    # 032: the ship rails join the goal toolset under AGENT_BUILDER_MODE (gates in
    # goal_tool_gates.py — bodies kept out of this file).
    from agents.task.goals.goal_tool_gates import (app_service_goal_tool_enabled,
                                                   publish_goal_tool_enabled)
    if publish_goal_tool_enabled() and "publish" not in tools:
        tools.append("publish")
    if app_service_goal_tool_enabled() and "app_service" not in tools:
        tools.append("app_service")
    return tools


def child_inheritable_tools() -> frozenset:
    """Posture-aware child-inheritable set. Posture 0: the frozen module constant.
    Posture>=1: + code_execution/shell so a self-decomposed compute goal isn't
    tool-starved (still sandbox-contained; money/social remain excluded)."""
    if _compute_posture_at_least_1():
        return CHILD_INHERITABLE_TOOLS | {"code_execution", "shell"}
    return CHILD_INHERITABLE_TOOLS


def imminent_cron_job(data_dir: str, now, headroom_sec: int) -> Optional[str]:
    """Name the cron job due within ``headroom_sec`` (overdue included), else None.

    Pure read of ``<data_dir>/cron.db``; ``headroom_sec <= 0`` or an absent /
    unreadable store answers None without touching disk (fail-open: dispatch).
    A goal run marks the shared project-root workspace busy and cron ticks skip
    until it ends, so the dispatcher uses this to let a due rail go FIRST."""
    if headroom_sec <= 0:
        return None
    from datetime import timedelta
    db_path = os.path.join(data_dir, "cron.db")
    if not os.path.exists(db_path):
        return None
    # Read-only over core.sqlite_util (the layering ratchet forbids agents -> cron);
    # the predicate mirrors CronJobStore.due() (enabled, scheduled, next_run_at set)
    # plus a job mid-run — it holds the workspace now, and its next_run_at is still
    # the past due time until it finishes.
    try:
        from core.sqlite_util import execute_retry
        rows = execute_retry(
            db_path,
            "SELECT id, task, next_run_at, status, payload FROM cron_jobs WHERE enabled=1 "
            "AND status IN ('scheduled','running') AND next_run_at IS NOT NULL "
            "AND next_run_at <= ? "
            "ORDER BY next_run_at",
            ((now + timedelta(seconds=headroom_sec)).isoformat(),), fetch="all")
    except Exception:
        logger.debug("imminent-cron probe failed; dispatching", exc_info=True)
        return None
    # 056 D1 follow-up (2026-09-19): a SCHEDULED money-class job pre-empts a running
    # goal at the step boundary (`yield_for_rail`), so with yield on it need not hold
    # the board back — after the D4 stagger the four rails' headroom windows covered
    # every minute of an even hour and two goals sat `ready` for 40 min. A job mid-run
    # holds the workspace NOW (nothing to pre-empt) and an ops-class job cannot
    # pre-empt at all, so both still defer.
    try:
        from core.config_policy.goal_flags import GoalFlagsMixin
        _yield_on = bool(GoalFlagsMixin.goal_yield_for_money_rail())
    except Exception:
        _yield_on = False
    for job_id, task, next_run_at, status, payload in rows or []:
        if _yield_on and status == "scheduled" and _payload_is_money(payload):
            continue
        return f"{str(job_id)[:12]} {(task or '')[:40]!r} due {next_run_at}"
    return None


def _payload_is_money(payload) -> bool:
    """Mirror of ``cron.jobs.is_money_job`` over the raw stored payload (the
    layering ratchet forbids agents -> cron)."""
    try:
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload or "{}")
        return str((payload or {}).get("priority") or "").lower() == "money"
    except Exception:
        return False


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
    so the resolved owner tenant is the sound representative tenant.
    Fail-open to None (=> legacy env-only value; see ``core.prefs.resolve``).

    Reads the ONE owner-tenant resolver (``resolve_owner_user_id``) so a pref the
    owner wrote from any seat is the pref this tick reads."""
    try:
        from core.instance import resolve_owner_user_id
        return resolve_owner_user_id()
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
        #: ONE worker id per process (claim_lock); hold_inflight() scopes its hold to it.
        self._worker = f"goal-dispatch-{os.getpid()}"
        #: 031: True once the paused edge was reconciled (hold + one log line).
        self._paused_seen = False
        self._quota_logged = False
        # §7.2 tail: the "consecutive empty planner runs" streak and the
        # once-per-stall escalation marker are DURABLE on the board
        # (GoalBoard.mark_planner_outcome / mark_stall_escalated). They were
        # instance attributes until 2026-08-29, when 14 service restarts in 36 h
        # (maintenance-loop deploys) re-armed the owner push on every restart.

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

    @staticmethod
    def dispatch_blocked_by_providers() -> bool:
        """True only when NO credentialed provider can serve right now.

        The old gate asked whether the DEFAULT provider was credit-dead, which
        conflates "my first choice died" with "I cannot work". Prod dispatched
        zero goals for 37 hours on that conflation while a second credentialed
        provider was available the whole time.

        Fail-open: a resolver error must never pause autonomy on its own.
        """
        try:
            from core.credit_sentinel import credit_sentinel_active
            from core.runtime_config import resolve_live_provider
            if resolve_live_provider(None) is not None:
                return False
            # Nothing resolved live — pause only when the sentinel is genuinely
            # tripped. An empty credential picture (test/dev, unreadable store) is
            # not credit death, and pausing on it would stop a working box.
            return bool(credit_sentinel_active(None))
        except Exception:
            logger.debug("provider liveness check failed — not pausing", exc_info=True)
            return False

    def _ready_for_dispatch(self, slots: int) -> list:
        """The ready goals this tick will claim — fair across objectives by default.

        Fail-open by design: any error in the fair path falls back to the legacy
        global ``board.ready`` order. A fairness bug must never be able to stop
        autonomous dispatch; an unfair tick is a far smaller failure than an idle
        board.
        """
        from agents.task.constants import AutonomyConfig
        if not AutonomyConfig.goal_fair_dispatch():
            return self.board.ready(limit=slots)
        cap = AutonomyConfig.goal_per_objective_cap()
        try:
            return self.board.ready_fair(
                limit=slots,
                per_objective_cap=cap,
                # ready_fair consults in_flight ONLY when a cap is set, so with the
                # default cap of 0 this GROUP BY would run every tick for a value
                # nothing reads.
                in_flight=self.board.count_running_by_objective() if cap > 0 else None)
        except Exception:
            logger.warning("fair dispatch failed — using the global ready order",
                           exc_info=True)
            return self.board.ready(limit=slots)

    async def dispatch_once(self) -> int:
        """Claim and run up to GOAL_MAX_CONCURRENT ready goals. Returns #dispatched.

        Cross-process safe: a single tick is gated by the cron-style TickLock so two
        workers don't both fan out the same ready set. ``reclaim_stale`` runs FIRST
        and UNCONDITIONALLY — before the ``GOALS_ENABLED`` gate (AU-F4.3) — so a
        crashed ``running`` row isn't stuck forever if the flag was flipped off.
        """
        from datetime import datetime
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

        # T2.1 final-review Fix 3: reconcile any stranded 'waiting' row —
        # same unconditional-maintenance-sweep placement as reclaim_stale/
        # age_out_blocked above (before the enabled-gate so a stranded row
        # doesn't stay stuck forever just because GOALS_ENABLED was
        # temporarily flipped off). Self-contained retry-safe SQL, cheap.
        try:
            reconciled = self.board.reconcile_waiting()
            if reconciled:
                logger.info("goal dispatch: reconciled %d stranded waiting goal(s)", reconciled)
        except Exception:
            logger.debug("waiting-goal reconciliation skipped", exc_info=True)

        if not AutonomyConfig.goals_enabled():
            return 0

        # 031 owner pause: ONE predicate (the legacy halt file/env are facets of
        # it). On the first denied tick after running, cancel this process's
        # in-flight runs and return their rows to ready — a pause is not a failure.
        from core.autonomy_control import allows
        _dec = allows("dispatch")
        if not _dec.allowed:
            if not self._paused_seen:
                self._paused_seen = True
                held = await self.hold_inflight(_dec.reason)
                logger.warning("goal dispatch PAUSED (%s) — held %d in-flight run(s)",
                               _dec.reason, len(held))
            return 0
        self._paused_seen = False

        # §6.3 provider-credit sentinel: while tripped (recent 402/credit-death),
        # burning more paid runs is pointless — pause dispatch until auto-release.
        try:
            # Ask "can ANYTHING serve?", not "is the default alive?". Asking only
            # about the default is why prod dispatched zero goals for 37 hours from
            # 2026-08-17 19:35Z: zai-coding was credit-dead and nothing ever checked
            # whether the second credentialed provider could carry the work. A dead
            # secondary must not pause a healthy one either (the 5.7h halt on
            # 2026-08-14) — resolve_live_provider covers both directions.
            if self.dispatch_blocked_by_providers():
                if not getattr(self, "_sentinel_logged", False):
                    logger.warning("goal dispatch paused: every credentialed provider "
                                   "is credit-dead")
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
            ready = self._ready_for_dispatch(slots)
            # A due cron rail goes first: on the shared workspace a goal run would
            # mark the process busy and hold that rail for its whole runtime.
            # Checked only when something is actually ready (an empty board must
            # not read cron.db and log a deferral every tick).
            if ready:
                _imminent = imminent_cron_job(
                    os.path.dirname(self.board.db_path), datetime.now(),
                    AutonomyConfig.goal_dispatch_cron_headroom_sec())
                if _imminent:
                    logger.info("goal dispatch deferred (%d ready): cron job %s",
                                len(ready), _imminent)
                    return 0
            ttl = AutonomyConfig.goal_claim_ttl_sec()
            worker = self._worker
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

    @staticmethod
    def _prior_artifacts(goal: Goal) -> list:
        """[(name, bytes), …] this goal's earlier attempts produced and that are
        STILL on disk unchanged.

        Verified through the ledger rather than listed from it, so a retry is
        never told to "continue from" a file the workspace cleanup removed — the
        exact lie that made round N+1 fail on round N's evidence. Fail-open: no
        ledger, no block, and the prompt is byte-identical to before.
        """
        try:
            import os as _os
            from core.artifacts import VERIFY_OK, get_artifact_ledger
            ledger = get_artifact_ledger()
            out = []
            for art in ledger.list_for_goal(goal.user_id, goal.id):
                if ledger.verify(art.id, goal.user_id) == VERIFY_OK:
                    out.append((_os.path.basename(art.path), art.bytes))
            return out
        except Exception:
            logger.debug("prior-artifact lookup skipped for %s", goal.id, exc_info=True)
            return []

    async def hold_inflight(self, reason: str) -> List[str]:
        """031: cancel every run this process owns (goal runs + planner runs),
        wait for them to unwind, then return their board rows to ``ready`` (no
        failure increment). The owner's chat session is never in ``_inflight``."""
        tasks = [t for t in list(self._inflight) if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            return self.board.hold_running(worker=self._worker, reason=reason)
        except Exception:
            logger.warning("hold_inflight: board hold failed", exc_info=True)
            return []

    async def yield_for_rail(self, job) -> List[str]:
        """056 WS5 (D1): a due MONEY cron job pre-empts this process's running goal
        runs — `hold_inflight` (cancel at the step boundary, rows back to `ready`,
        no failure increment) plus a `resume_note` so the goal knows why it
        restarts and where its own artefacts are. Returns the held goal ids."""
        jid = str(getattr(job, "id", "") or "")
        jtask = str(getattr(job, "task", "") or "")[:40]
        held = await self.hold_inflight(f"money rail {jid} ({jtask}) due")
        from datetime import datetime as _dt
        stamp = _dt.utcnow().strftime("%H:%MZ")
        for gid in held:
            try:
                self.board.merge_payload(gid, {
                    "resume_note": (f"Yielded at {stamp} because the money rail {jtask} "
                                    f"({jid[:8]}) came due; this is a RESTART — read what "
                                    f"you already wrote to disk before redoing anything.")})
                self.board._event(gid, "yielded", {"job_id": jid, "task": jtask})
            except Exception:
                logger.debug("yield note failed for %s", gid, exc_info=True)
        return held

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
        worker = self._worker
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
            raise
        try:
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
                _g = self.board.record_failure(
                    goal.id,
                    error=f"{LLM_EXHAUSTED_MARKER}: no live provider for this goal "
                          f"(pinned={payload.get('provider') or '(none)'!r}, "
                          f"default={default_provider!r}, both credit-dead)")
                await self._maybe_escalate_blocked(_g, block_kind_hint="provider_outage")
                return
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
                "task": build_goal_run_task(goal, objective,
                                            workspace_root=_deliverables_root(),
                                            prior_artifacts=self._prior_artifacts(goal)),
                "provider": provider,
                "model": model,
                "tools": self._resolve_tools(goal),
                "max_steps": payload.get("max_steps", AutonomyConfig.goal_default_max_steps()),
                "temperature": 0.0,
                "goal_id": goal.id,
            }
            # 044 T20: a goal carrying `payload.group` SERVICES a room — the run
            # session IS the room's bound session (PUBLIC profile + room toolset,
            # applied by bind_chat_surface), and its task is the room's ledger
            # tail since THIS reader's checkpoint. An empty tail is a $0 skip:
            # nothing was asked, so no model is paid to discover that.
            from agents.task.goals.group_service import (
                build_service_task, close_room_books, room_binding,
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
                    self.board.record_success(
                        goal.id, result=f"skipped: {_skip} — no service work for "
                                        f"{_src.surface_id}:{_src.chat_id} this run")
                    _goal_ev(goal, "skipped", _skip)
                    return
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
                _g = self.board.record_failure(goal.id, error=_gate_err)
                await self._maybe_escalate_blocked(_g)
                return
            # Route through the shared helper: create_session → run_session → RunOutcome.
            # H11: hard wall-clock cap (mirrors cron's per-job wait_for). max_steps alone
            # doesn't bound wall time — a single hung step would occupy a slot forever. On
            # timeout the TimeoutError is handled by the except below (record_failure) and
            # the finally cancels the claim heartbeat, so reclaim_stale can recover the slot.
            _max_run = AutonomyConfig.goal_max_run_seconds()
            run = None
            try:
                run = await asyncio.wait_for(
                    _run_task_to_outcome(
                        self.task_agent, user_id=goal.user_id, request=request,
                        autonomous=True, goal_id=goal.id, creator="goal",
                    ),
                    timeout=_max_run,
                )
            finally:
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
            session_id = run.session_id
            if session_id is None:
                _g = self.board.record_failure(goal.id, error="create_session returned no id")
                await self._maybe_escalate_blocked(_g)
                return
            # Attribute this run's artifacts to the goal BEFORE any exit branch, so a
            # run that failed (out of steps, blocked, refused) keeps the evidence it
            # really produced. 46 goals died on "ran out of steps" last week and each
            # retry then restarted blind against a workspace whose files it could no
            # longer attribute. Fail-open: bookkeeping never fails a finished run.
            try:
                from core.artifacts import get_artifact_ledger
                get_artifact_ledger().attach_goal(goal.user_id, session_id, goal.id)
            except Exception:
                logger.debug("artifact goal attribution skipped for %s", goal.id,
                             exc_info=True)
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
                if RUN_BUDGET_MARKER in str(run.status or ""):
                    error = f"{RUN_BUDGET_MARKER}: {str(run.status)[:400]}"
                elif _is_llm_provider_exhausted(run.status):
                    error = f"{LLM_EXHAUSTED_MARKER}: {str(run.status)[:400]}"
                    block_kind_hint = "provider_outage"
                _g = self.board.record_failure(
                    goal.id, error=error, session_id=session_id)
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
                    await self._fail_run(
                        goal, session_id,
                        error=("run ended without completing (no done() — likely ran "
                               "out of steps)" + detail + gap_note)[:2000],
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
                    error=("done() after every action errored — nothing executed "
                           "successfully" + gap_note)[:2000],
                    outcome=outcome,
                    steps=run.steps, spend_usd=run.spend_usd,
                    artifacts=run.artifacts)
                return
            if checks and check_results is None:
                check_results = await self._evaluate_acceptance(
                    checks, goal, session_id, run)
            if check_results:
                _failed = failed_checks(check_results)
                if _failed:
                    await self._fail_run(
                        goal, session_id,
                        error=("acceptance checks failed: " + "; ".join(
                            f"{c.get('type')}: {c.get('detail')}" for c in _failed)
                            + gap_note)[:2000],
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
            # FIX 6: a salvaged (no-done()) run has NO completion claim, and the
            # judge exists to weigh a claim against the evidence. The typed checks
            # already passed deterministically — that IS the verdict, so skip the
            # (paid) judge rather than let it invent an UNMET for a missing claim.
            if judge_on and not salvaged_without_done:
                verdict, reason = await _cj.judge_run_outcome(
                    self.task_agent, session_id, goal, run)
                if verdict == _cj.VERDICT_UNMET:
                    await self._fail_run(
                        goal, session_id,
                        error=(f"completion judge: {reason} "
                               f"(claim contradicted by evidence)" + gap_note)[:2000],
                        outcome=outcome,
                        steps=run.steps, spend_usd=run.spend_usd,
                        artifacts=run.artifacts)
                    return
                run.verified = "verified" if verdict == _cj.VERDICT_MET else "unverified"
            elif salvaged_without_done:
                run.verified = "verified"  # every declared acceptance check passed
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
                                              verified=run.verified if judge_on else "verified",
                                              artifacts=run.artifacts)
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
            block_kind_hint = None
            if _is_llm_provider_exhausted(e):
                error_text = f"{LLM_EXHAUSTED_MARKER}: {error_text}"[:2000]
                block_kind_hint = "provider_outage"
            _goal_ev(goal, "failed", reason=error_text[:200], session_id=session_id)
            try:
                _g = self.board.record_failure(goal.id, error=error_text, session_id=session_id)
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
        finally:
            hb_task.cancel()
            if _shared_ws:
                from core.interactive_gate import mark_idle
                mark_idle()

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
        _g = self.board.record_failure(goal.id, error=error, session_id=session_id)
        if block and getattr(_g, "status", None) == STATUS_READY:
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
        block_kind_hint = "provider_outage" if _is_llm_provider_exhausted(error) else None
        # T4-03: surface the verified-failure outcome (blocked vs failed) in the durable
        # event log so `polyrob telemetry` reflects it, not just the episodes table.
        _goal_ev(goal, "blocked" if getattr(_g, "status", None) == STATUS_BLOCKED else "failed",
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

    def _resolve_tools(self, goal: Goal) -> list:
        """Resolve the toolset for a goal run.

        Precedence: the goal's own ``payload.tools`` if set; else, for a CHILD goal
        (has ``parent_id``), inherit the parent's tools intersected with
        ``CHILD_INHERITABLE_TOOLS`` (so a self-decomposed goal isn't tool-starved
        while never self-granting money/social tools); else the safe default.
        """
        payload = goal.payload or {}
        # 044 T20: a ROOM service run is PUBLIC — its audience is many humans, so
        # the toolset is the room's read-only one and nothing in the payload can
        # widen it. Said out loud rather than silently dropped: a goal that asked
        # for `defi_trade` must leave a line naming the refusal.
        if payload.get("group"):
            from core.surfaces.room_policy import room_tool_ids
            if payload.get("tools"):
                logger.warning(
                    "goal %s: payload.tools ignored — a room service run uses the "
                    "room toolset (%s)", goal.id, payload.get("tools"))
            return room_tool_ids()
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

    async def _maybe_escalate_blocked(self, goal, *, agent_reported: bool = False,
                                      block_kind_hint: Optional[str] = None) -> None:
        """§7.2: when record_failure tripped the breaker (goal now 'blocked'), surface
        a concrete ask to the owner instead of letting it die silently. Fail-open.

        §3.4: the escalation PUSH is a safety net — when the agent itself already
        reported the block to its user during the run (``agent_reported``, from
        RunOutcome.user_messages), the push is skipped; the durable ask below is
        ALWAYS left either way.

        T2.1 Task-3 review fix (finding #1, CRITICAL): this is the SINGLE
        block_kind stamping choke point. ``block_kind_hint`` lets a caller that
        already classified the failure (``_fail_run``, plus the two call sites
        that bypass it entirely — the refusal-status path and the top-level
        ``except Exception`` handler in ``_run_goal``) stamp that classification
        here, ``only_if_absent`` — BEFORE the generic ``needs_input`` fallback
        below, so a real provider death is never mis-labeled ``needs_input``
        (which kind-aware aging leaves blocked with no auto-requeue at all)."""
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
                # T2.1 Task 3 (review-fixed): stamp the CALLER's own
                # classification first (only_if_absent — never clobbers a more
                # specific kind a producer already stamped, e.g. dep_failed
                # from the DAG cascade), THEN fall back to the generic "needs
                # an owner decision" kind only if still nothing more specific
                # is set. Both stamps are only_if_absent, so this NEVER clobbers.
                if block_kind_hint:
                    try:
                        self.board.stamp_block_kind(
                            goal.id, block_kind_hint, only_if_absent=True)
                    except Exception:
                        logger.debug("block_kind hint stamp skipped for %s",
                                     goal.id, exc_info=True)
                try:
                    self.board.stamp_block_kind(
                        goal.id, "needs_input", only_if_absent=True)
                except Exception:
                    logger.debug("needs_input stamp skipped for %s", goal.id, exc_info=True)
                self.board.create_ask(
                    user_id=goal.user_id,
                    what=f"Unblock goal: {goal.title}",
                    why=(goal.last_failure_error or "repeated failures"),
                    blocks_goal_ids=[goal.id],
                )
        except Exception:
            logger.debug("blocked-goal ask creation skipped", exc_info=True)

    @staticmethod
    def _wake_text(goal: Goal, final: str, verified: str = "verified") -> str:
        """056 WS7: what a completed goal says when it re-enters the session that
        CREATED it. Default = ONE line (`goal <id8> done — <first line>`, ≤280
        chars) — the origin session already has the artefacts on disk and a full
        result re-entering it cost the owner a 5-step "verification" turn
        (2026-09-19 03:00Z). `payload.report_back=true` (goal_create) keeps the
        full completion text for goals whose whole point is the report."""
        payload = goal.payload or {}
        if payload.get("report_back"):
            head = (f"✅ Background goal '{goal.title}' completed." if verified == "verified"
                    else f"Background goal '{goal.title}' finished — done (unverified).")
            return f"{head}\nResult:\n{str(final)[:1500]}"
        first = ""
        for line in str(final or "").splitlines():
            if line.strip():
                first = line.strip()
                break
        state = "done" if verified == "verified" else "done (unverified)"
        text = f"goal {str(goal.id)[:8]} {state} — {first}"
        return text if len(text) <= 280 else text[:277] + "…"

    def _completion_text(self, goal: Goal, final: str, verified: str = "verified",
                         deliverable_lines: Optional[list] = None,
                         session_link: Optional[str] = None) -> str:
        # §4.3: the ✅ is EARNED — an unverified completion is labeled honestly,
        # never pushed as a green checkmark on an unchecked claim.
        if verified == "verified":
            head = f"✅ Background goal '{goal.title}' completed."
        else:
            head = f"Background goal '{goal.title}' finished — done (unverified)."
        parts = [f"{head}\nResult:\n{str(final)[:1500]}"]
        # QW-1 (proposal 021): every artifact the run produced is accounted for
        # — attached (rail media) or listed server-only — never a bare filename.
        if deliverable_lines:
            parts.append("Deliverables:\n" + "\n".join(deliverable_lines))
            # Publishing evaluation 2026-09-05: "done" must not read as "reachable"
            # when nothing is at a URL — one honest line, naming the switch if off.
            from agents.task.goals.deliverables import (publish_rail_available,
                                                        reachability_note)
            note = reachability_note(
                deliverable_lines,
                rail_available=publish_rail_available(
                    getattr(self.task_agent, "container", None)))
            if note:
                parts.append(note)
        if session_link:
            parts.append(f"Console: {session_link}")
        return "\n".join(parts)

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
                                 verified: str = "verified",
                                 artifacts: Optional[list] = None) -> bool:
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
            # QW-1 (proposal 021): build the deliverables block + attachments
            # from the run's artifact registry. Attaching is flag-gated
            # (DELIVERABLES_ATTACH_ENABLED, ON under POLYROB_LOCAL); the honest
            # text listing rides regardless. Fail-open to the legacy text push.
            deliverable_lines: list = []
            attachments: list = []
            if artifacts:
                try:
                    from core.config_policy import AutonomyConfig as _AC
                    from agents.task.goals.deliverables import build_deliverables
                    attachments, deliverable_lines = build_deliverables(
                        artifacts, session_id, goal.user_id,
                        attach=_AC.deliverables_attach_enabled())
                except Exception:
                    logger.debug("deliverables build skipped for %s", goal.id,
                                 exc_info=True)
            # Security review F4: the push delivers to the INSTANCE OWNER
            # principal — if the goal's tenant is a DIFFERENT known principal,
            # media must never ride (cross-tenant artifact leak). The text
            # lines keep the files reachable (they carry server paths).
            if attachments:
                try:
                    from core.instance import resolve_owner_principal
                    _owner = str(resolve_owner_principal() or "")
                    if _owner and str(goal.user_id or "") and \
                            str(goal.user_id) != _owner:
                        attachments = []
                except Exception:
                    attachments = []  # unknown => fail-closed for media
            session_link = None
            try:
                from core.surfaces.deep_link import webview_session_link
                session_link = webview_session_link(session_id)
            except Exception:
                pass
            text = self._completion_text(goal, final, verified=verified,
                                         deliverable_lines=deliverable_lines,
                                         session_link=session_link)
            container = getattr(self.task_agent, "container", None)
            if attachments:
                owner_told = await push_owner_message(container, text,
                                                      attachments=attachments)
            else:
                owner_told = await push_owner_message(container, text)
        except Exception:
            logger.debug("goal completion owner-push skipped for %s", goal.id, exc_info=True)
        if owner_told:
            self._mark_episode_surfaced(goal, session_id)
        return owner_told

    async def _self_wake(self, goal: Goal, session_id: str, final: str) -> None:
        """Agent-continuation (W1 rail): re-enter the session that CREATED this goal
        (``payload.origin_session_id``, stamped by ``goal_create``) as a forged turn,
        so the session that asked for the work learns it finished. OWNER notification
        is handled separately by ``_notify_owner_done`` (always-on). Fail-open.

        Never the run session itself: it just produced ``final`` and has nothing to
        act on. Prod 2026-08-24..28 woke the run session 134 times and 126 of those
        turns closed as "self-wake is just the completion echo of the goal I already
        finished this session" — 9.25M input tokens for nothing. A framework-seeded
        goal (stream/planner/operator) has no origin session and gets no wake.
        """
        origin = str((goal.payload or {}).get("origin_session_id") or "").strip()
        if not origin or origin == str(session_id or ""):
            logger.debug("goal %s: no distinct origin session — no self-wake (run "
                         "session already holds its own result)", goal.id)
            return
        try:
            deliver = getattr(self.task_agent, "deliver_self_wake", None)
            if deliver is None:
                return
            delivered = await deliver(origin, goal.user_id,
                                      self._wake_text(goal, final, "verified"),
                                      metadata={"source": "goal", "goal_id": goal.id,
                                                "run_session_id": session_id})
            if delivered:
                self._mark_episode_surfaced(goal, session_id)
        except Exception as e:
            logger.debug("goal self-wake skipped for %s: %s", goal.id, e)

    async def _maybe_plan(self, *, headroom_after: int) -> None:
        """Fire ONE planning session when the queue is thin. All gates mechanical."""
        from agents.task.constants import AutonomyConfig
        from agents.task.goals.planner import planner_ceilings
        if not AutonomyConfig.goal_planner_enabled():
            return
        # 031: the `planner` scope of the owner pause (dispatch may still run).
        from core.autonomy_control import allows
        if not allows("plan").allowed:
            return
        # Resolve the standing objectives FIRST: the thinness gate below scales with
        # how many streams exist. A fixed floor of 2 means a 16-stream board must
        # nearly empty before the planner may refill it, and by then most streams
        # have been dark for days.
        # any tenant with an active objective (v1: single-user) — this list is
        # CROSS-TENANT while the prompt built below is per-tenant, which is why
        # the run is pinned to sorted(users)[0]. Both the count fed to
        # planner_ceilings and that pick assume one owner; a real multi-tenant
        # board needs a planner run per tenant, not a wider ceiling.
        active = self._active_objective_owners()
        users = {o.user_id for o in active}
        if not users:
            return
        # An objective at its lifetime goal budget is STALLED: the planner can
        # open nothing on it, and every prompt-shaped nudge to "raise an ask"
        # depends on the model complying (prod 2026-08-28: two objectives sat at
        # 25/25 for days, silently). Escalate deterministically, here, where the
        # active objectives are already loaded. Idempotent + fail-open.
        for _uid in sorted(users):
            try:
                self.board.escalate_spent_objectives(user_id=_uid)
            except Exception as e:
                logger.debug("spent-objective escalation skipped for %s: %s", _uid, e)
        min_ready = AutonomyConfig.goal_planner_min_ready()
        if AutonomyConfig.goal_planner_scaling():
            ceilings = planner_ceilings(len(active))
            min_ready = max(min_ready, min(len(active), ceilings["ready_ceiling"]))
        if len(self.board.ready(limit=min_ready)) >= min_ready:
            return
        # FIX 5: the planner runs for ONE tenant (see the cross-tenant note above),
        # so every piece of its bookkeeping — cooldown, backoff streak, stall
        # dedup — is keyed to THAT tenant. Resolved before the first read.
        user_id = sorted(users)[0]
        last = self.board.last_planner_run_at(user_id=user_id)
        cooldown = AutonomyConfig.goal_planner_cooldown_sec()
        # Back off after consecutive empty runs (planner_backoff_multiplier): an
        # hourly run that keeps concluding "nothing to add" is paid repetition.
        try:
            from agents.task.goals.planner import planner_backoff_multiplier
            cooldown *= planner_backoff_multiplier(
                self.board.consecutive_empty_planner_runs(user_id=user_id))
        except Exception:
            logger.debug("planner backoff lookup failed (using base cooldown)",
                         exc_info=True)
        import time as _time
        # The board's clock, not time.time(): mark_planner_run stamps with it, so
        # the comparison must read the same clock (and tests can inject one).
        now = getattr(self.board, "_now", _time.time)()
        if last is not None and (now - last) < cooldown:
            return
        # BEFORE dispatch: no double-fire while running
        self.board.mark_planner_run(user_id=user_id)
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
            from core.runtime_config import resolve_default_provider
            provider = resolve_default_provider()[0]
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
                self.task_agent, user_id=user_id, request=request, autonomous=True,
                creator="goal")
            # 015 #3 (planner leg): a planner run killed by provider exhaustion
            # must not read as "planner correctly found nothing to do" — that
            # ambiguity hid a 13h board-dark outage from two intel reviews.
            from core.credit_sentinel import credit_sentinel_active
            if _is_llm_provider_exhausted(final or "") or \
                    (not final and credit_sentinel_active(provider)):
                logger.error(
                    "%s: goal planner run died on provider outage, NOT an "
                    "empty pipeline (session=%s): %s",
                    LLM_EXHAUSTED_MARKER, session_id,
                    (final or "no output")[:200])
                return
            logger.info("goal planner ran (session=%s): %s",
                        session_id, (final or "no result")[:200])
            # Read the outcome from the BOARD, not the model's summary text: how
            # many goals this session actually created, and whether anything is
            # still in flight. Both persist (backoff + once-per-stall marker).
            try:
                queued = self.board.count_created_by_session(str(session_id))
                live = 1 if self.board.has_live_goals(user_id=user_id) else 0
                self.board.mark_planner_outcome(queued=queued, live=live,
                                                user_id=user_id)
            except Exception:
                logger.debug("planner outcome not recorded", exc_info=True)
            await self._maybe_escalate_empty_pipeline(user_id, planner_summary=final)
        except Exception as e:
            if _is_llm_provider_exhausted(e):
                logger.error("%s: goal planner run failed on provider outage: %s",
                             LLM_EXHAUSTED_MARKER, e)
            else:
                logger.error("goal planner run failed: %s", e, exc_info=True)

    async def _maybe_escalate_empty_pipeline(self, user_id: str, *,
                                             planner_summary: Optional[str] = None) -> None:
        """§7.2 tail: a planner run that STILL leaves the board with NO work is a
        stall — surface it to the owner exactly once per stall. Fail-open.

        What is NOT a stall (2026-08-29 forensics — prod pushed "my pipeline is
        empty" twice in five hours while the trading stream had run ten clean
        cycles):
        - work in flight (running/waiting/triage legs of a cycle) — only
          ``ready`` was checked before;
        - a manifest stream idle BETWEEN cycles (its cadence window reopens at a
          known time — ``streams.next_seed_at``);
        - every active objective either a stream or at its goal budget with the
          spent-objective ask already open — the owner has the decision, there
          is nothing new to say.
        The push names an objective that could actually take work (starved order),
        never the first row by priority (which was a budget-spent one). The
        streak and the once-per-stall marker are durable on the board, so a
        restart cannot re-arm the push.
        """
        try:
            from agents.task.constants import AutonomyConfig
            if self.board.has_live_goals(user_id=user_id):
                return
            runs = self.board.consecutive_stall_runs(user_id=user_id)
            if runs < AutonomyConfig.goal_empty_pipeline_escalate_after():
                return
            streak_started = self.board.empty_streak_started_at(user_id=user_id)
            if self.board.stall_escalated_since(streak_started, user_id=user_id):
                return
            import time as _time
            now = _time.time()
            streams = self._declared_streams()
            if streams:
                from agents.task.goals.streams import next_seed_at
                reopens = next_seed_at(self.board, user_id, streams, now)
                if reopens is not None and reopens > now:
                    logger.info(
                        "goal pipeline idle, not stalled: the next stream seed is due "
                        "at %s", _time.strftime("%Y-%m-%d %H:%MZ", _time.gmtime(reopens)))
                    return
            servable = self._servable_objectives(user_id, streams=streams, now=now)
            if servable is not None and not servable:
                logger.info("goal pipeline covered, not stalled: every objective is a "
                            "stream or at its goal budget with its ask open")
                return
            objective_title = servable[0].title if servable else None
            # Mark the stall escalated once the threshold is reached, independent of
            # whether the owner PUSH lands — the durable ask below is the owner-visible
            # artifact and must be created even under the silent posture.
            self.board.mark_stall_escalated(user_id=user_id)
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

    def _declared_streams(self) -> list:
        """The manifest's streams, or ``[]`` when there is no readable manifest.
        Fail-open: an unreadable manifest must never block an escalation."""
        try:
            from agents.task.goals.streams import default_manifest_path, load_manifest
            path = default_manifest_path()
            if not path or not os.path.exists(path):
                return []
            return load_manifest(path)
        except Exception:
            logger.debug("stream manifest unavailable to the dispatcher", exc_info=True)
            return []

    def _servable_objectives(self, user_id: str, *, streams: Optional[list] = None,
                             now: Optional[float] = None):
        """Active objectives that could take a new goal right now, hungriest first;
        ``[]`` when every objective is covered; ``None`` if the board read failed.

        Covered = a manifest stream that is busy, inside its cadence window, or due
        for less than ``SEEDER_GRACE_SEC`` (the hourly seeder will refill it); or an
        objective at its goal budget whose spent-objective ask is already open (the
        owner holds the decision). A stream objective whose stream is NOT in the
        manifest, or is overdue past the grace, is a stall and stays servable so
        the escalation names it. Order: fewest in-flight children, oldest activity
        — the same order the planner is told to serve.
        """
        try:
            from agents.task.goals.board import ASK_OPEN, OBJ_ACTIVE
            from agents.task.goals.planner import _starved_order_with_stats
            from agents.task.goals.streams import SEEDER_GRACE_SEC, stream_overdue_by
            objectives = self.board.objectives(user_id=user_id, status=OBJ_ACTIVE)
            open_spent = {(a.payload or {}).get("objective_id")
                          for a in self.board.asks(user_id=user_id, status=ASK_OPEN)
                          if (a.payload or {}).get("kind")
                          == self.board.ASK_KIND_OBJECTIVE_SPENT}
            by_id = {str(st.get("id")): st for st in (streams or [])}
            servable = []
            for o in objectives:
                sid = (o.payload or {}).get("stream_id")
                if sid:
                    st = by_id.get(str(sid))
                    if st is not None and stream_overdue_by(
                            self.board, user_id, st, now) <= SEEDER_GRACE_SEC:
                        continue  # the stream's own cadence covers it
                    servable.append(o)
                    continue
                budget = self.board.objective_budget(o)
                if budget > 0 and o.id in open_spent and \
                        len(self.board.children_of(user_id, o.id)) >= budget:
                    continue
                servable.append(o)
            return [o for o, _live, _last in
                    _starved_order_with_stats(self.board, user_id, servable)]
        except Exception:
            logger.debug("servable-objective scan failed", exc_info=True)
            return None


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
