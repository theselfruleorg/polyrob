"""Start/stop the autonomy background loops (cron, goals, curator) for ANY entry.

Previously these tickers were inlined in api/app.py's lifespan, so the terminal
(`rob`) never ran them. This module is the single shared place both the FastAPI
server and the CLI REPL call. Each loop is independently gated and fail-open: one
loop failing to build never blocks the others. Idempotent stop()."""
from __future__ import annotations

import asyncio
import logging
from core.config_policy import AutonomyConfig, _mode_capability_default
from typing import List, Tuple

logger = logging.getLogger(__name__)

_STOP_GRACE_SEC = 5.0  # bound the graceful per-loop wind-down before force-cancel

#: Strong references to in-flight fire-and-forget background tasks (currently
#: just the cold-start orphan sweep). asyncio only holds a WEAK reference to a
#: task created via ``asyncio.create_task`` — without this, the task object can
#: be garbage-collected mid-run (a well-known asyncio footgun), silently
#: dropping the sweep. Self-cleans via ``add_done_callback``.
_BACKGROUND_TASKS: set = set()


def _cron_enabled() -> bool:
    from tools.cronjob_tools import cron_enabled
    return cron_enabled()


def _goals_enabled() -> bool:
    return AutonomyConfig.goals_enabled()


def _curator_enabled() -> bool:
    return AutonomyConfig.curator_enabled()


def _surface_gc_enabled() -> bool:
    from agents.task.surface_config import SurfaceConfig
    return SurfaceConfig.surface_gc_enabled()


def _x402_invoicing_enabled() -> bool:
    # Read the env directly (core.env SSOT) — importing modules.x402 here would
    # put a server-tier module on the core import graph (C3 boundary), so this
    # can't share modules.x402.invoicing.x402_invoicing_enabled (the SSOT the
    # x402_invoice tool and the settlement/pay endpoints both use). Apply the
    # same guarded-OR locally instead (013 T2 review fix, Finding 2 — this was
    # raw-env-only, so the settlement watcher never started under autonomous
    # mode even though invoices were creatable, i.e. unsettleable invoices).
    from core.env import bool_env
    try:
        default = _mode_capability_default("X402_INVOICE_ENABLED")
    except Exception:
        default = False
    return bool_env("X402_INVOICE_ENABLED", default)


def _hf_deploy_enabled() -> bool:
    from tools.hf_deploy import hf_deploy_enabled
    return hf_deploy_enabled()


_SURFACE_GC_INTERVAL_SEC = 3600  # hourly

_QUIET_RELEASE_INTERVAL_SEC = 300  # window-end precision of ~5 min


def _quiet_release_enabled() -> bool:
    """018 P0.3: sweep quiet-held deliveries whenever the user-delivery rail is
    on (the hold can only be produced by that rail) and prefs are enabled (the
    window is a pref; with prefs off nothing can be held)."""
    from core.prefs import prefs_enabled
    from core.surfaces.user_delivery import send_message_user_delivery_enabled
    return send_message_user_delivery_enabled() and prefs_enabled()


def _build_quiet_release_ticker(task_agent):
    """018 P0.3 (owner decision: defer-to-window-end): deliver messages the
    quiet-hours gate held once their tenant's window ends. The sweep is
    idempotent (released messages record a consumed outcome under the same
    content hash) and a tick must never disrupt the runtime."""
    from core.tickers import IntervalTicker

    async def _tick():
        try:
            from core.surfaces.user_delivery import release_quiet_held
            container = getattr(task_agent, "container", None)
            released = await release_quiet_held(container)
            if released:
                logger.info("quiet-hours release delivered %d held message(s)",
                            released)
        except Exception as e:
            logger.debug("quiet release tick failed: %s", e)

    return IntervalTicker(_tick, interval_seconds=_QUIET_RELEASE_INTERVAL_SEC)


def _build_surface_gc_ticker(task_agent):
    """a5: periodically purge stale chat<->session bindings so the routing map can't grow
    unboundedly. Resolves the registry from the task_agent's container (no extra
    plumbing through start_autonomy); fail-open and no-op when the chat bus is off."""
    from core.tickers import IntervalTicker
    from agents.task.surface_config import SurfaceConfig

    async def _tick():
        try:
            from core.surfaces.gc import purge_stale_safe
            container = getattr(task_agent, "container", None)
            # A7 (2026-07-13 review): correspondent TTL purge — purge_expired had no
            # production caller, so bindings never expired. Opt-in via
            # CORRESPONDENT_TTL_DAYS (>0); runs even when the chat registry is absent.
            try:
                ttl_days = SurfaceConfig.correspondent_ttl_days()
                if ttl_days > 0:
                    corr = (container.get_service("correspondent_registry")
                            if container else None)
                    if corr is not None and hasattr(corr, "purge_expired"):
                        expired = corr.purge_expired(ttl_days * 86400)
                        if expired:
                            logger.info(
                                "surface GC expired %d idle correspondent binding(s) "
                                "(TTL %dd)", expired, ttl_days)
            except Exception as e:
                logger.debug("correspondent TTL purge skipped: %s", e)
            registry = container.get_service("session_chat_registry") if container else None
            if registry is None or not hasattr(registry, "purge_stale"):
                return
            queue = None
            try:
                queue = container.get_service("outbound_queue") if container else None
            except Exception:
                pass
            horizon = SurfaceConfig.surface_gc_horizon_secs()
            removed = purge_stale_safe(registry, queue, horizon)
            if removed:
                logger.info("surface GC purged %d stale chat binding(s)", removed)
        except Exception as e:  # a GC tick must never disrupt the runtime
            logger.debug("surface GC tick failed: %s", e)

    return IntervalTicker(_tick, interval_seconds=_SURFACE_GC_INTERVAL_SEC)


def _build_cron_ticker(task_agent, data_dir):
    from cron.runner import build_cron_ticker
    return build_cron_ticker(task_agent, data_dir=data_dir)


def _build_goal_ticker(task_agent, data_dir):
    from agents.task.goals.dispatcher import build_goal_ticker
    # §5.1 cold-start sweep: re-queue goals left `running` by the previous
    # process WITHOUT a failure increment — a deploy/restart is not the goal's
    # fault (two deploys mid-goal used to silently block it via reclaim_stale's
    # crash accounting). Runs once, before the first tick. Fail-open.
    try:
        import os as _os
        from agents.task.goals.board import GoalBoard
        n = GoalBoard(_os.path.join(data_dir, "goals.db")).requeue_running_on_boot()
        if n:
            logger.info("cold-start goal sweep: re-queued %d running goal(s)", n)
    except Exception:
        logger.debug("cold-start goal sweep skipped", exc_info=True)
    return build_goal_ticker(task_agent, data_dir=data_dir)


def _build_curator_ticker(data_dir):
    from agents.task.agent.core.curator import build_curator_ticker
    return build_curator_ticker(data_dir=data_dir)


def _build_settlement_watcher(task_agent):
    # Lazy server-tier import — only executes when X402_INVOICE_ENABLED is on,
    # so a rob-core-only environment never touches modules.x402.
    from modules.x402.settlement_watcher import build_settlement_watcher
    return build_settlement_watcher(task_agent)


def _schedule_cold_start_orphan_reap() -> None:
    """P1-B review (Important #2) — reap ``polyrob.sandbox=1``-labeled persistent
    sandbox containers a previous, crashed process left running (a process that
    dies without calling ``DockerBackend.teardown()`` orphans its container; see
    ``tools/code_exec/backends/docker.py::reap_orphans``).

    ⚠️ COLD START ONLY. This must run exactly ONCE, right here, at process
    start — NEVER wire it into a recurring/periodic ticker. A periodic sweep
    would force-kill containers belonging to sessions that are simply idle
    between turns (which can legitimately outlast any sane ``max_age_sec``),
    misreading "idle" as "orphaned". One sweep at boot catches the
    crash-recovery case without ever touching a live session.

    No-op — not even a ``docker ps`` call — unless persistent mode is actually
    enabled AND the ``docker`` CLI is present. Fail-open: any error (sync or
    async) is logged and swallowed, never allowed to disrupt startup.
    """
    import shutil
    from tools.code_exec import code_exec_docker_persistent_enabled

    if not code_exec_docker_persistent_enabled():
        return
    if shutil.which("docker") is None:
        return

    async def _sweep() -> None:
        try:
            from tools.code_exec.backends.docker import DockerBackend
            removed = await DockerBackend.reap_orphans()
            if removed:
                logger.info(
                    "cold-start orphan sweep: removed %d stale persistent sandbox container(s)",
                    removed,
                )
        except Exception as e:
            logger.warning("cold-start orphan sweep failed (non-fatal): %s", e)

    task = asyncio.create_task(_sweep())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


_boot_migrations_scheduled = False


def _schedule_boot_migrations(task_agent) -> None:
    """D2 (2026-07-14 review): boot migrations ran ONLY in the API lifespan
    (api/app.py), so the headless/CLI postures — ``polyrob telegram``, the chat
    REPL, email/gateway — never migrated bot.db: code deployed ahead of schema
    hit "no such column" at runtime with no self-heal. Schedule the same
    fail-open, snapshot-first ``run_boot_migrations`` for every posture that
    starts autonomy. Once per process; a container without a
    ``database_manager`` makes it a no-op (run_boot_migrations handles that),
    and the API lifespan having already migrated makes it an idempotent no-op.
    """
    global _boot_migrations_scheduled
    if _boot_migrations_scheduled:
        return
    _boot_migrations_scheduled = True
    try:
        container = getattr(task_agent, "container", None)
        if container is None:
            return

        async def _migrate() -> None:
            try:
                from migrations.boot import run_boot_migrations
                summary = await run_boot_migrations(container, local=True)
                applied = (summary or {}).get("applied") or []
                if applied:
                    logger.info("boot migrations applied: %s", ", ".join(applied))
            except Exception as e:
                logger.warning("boot migrations failed (non-fatal): %s", e)

        coro = _migrate()
        try:
            task = asyncio.create_task(coro)
        except Exception:
            coro.close()  # avoid a "coroutine was never awaited" leak on failure
            raise
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
    except Exception as e:
        logger.warning("Could not schedule boot migrations: %s", e)


_owner_profile_seed_scheduled = False


def _schedule_owner_profile_seed(task_agent) -> None:
    """G-1 (metering finalization): seed the owner/local user_profiles row(s)
    once per process so FK-constrained metering writes (usage_records ->
    user_profiles) don't raise IntegrityError on a headless/single-owner
    deployment where nothing else seeds user_profiles until an external
    onboarding event. Covers the API-lifespan and CLI-REPL autonomy-start
    entry seams; the orchestrator construction seam
    (agents/task/agent/orchestrator.py::_maybe_seed_owner_profile) covers
    plain chat sessions when autonomy isn't running.

    Fail-open, once-per-process (module-level flag, mirrors the orchestrator
    seam's guard) — never raises, never blocks startup.
    """
    global _owner_profile_seed_scheduled
    if _owner_profile_seed_scheduled:
        return
    _owner_profile_seed_scheduled = True
    try:
        db = None
        container = getattr(task_agent, "container", None)
        if container is not None and hasattr(container, "get_service"):
            db = container.get_service("database_manager")

        async def _seed() -> None:
            try:
                from modules.database.user_profiles import ensure_owner_profile
                await ensure_owner_profile(db=db)
            except Exception as e:
                logger.warning("owner profile seed failed (non-fatal): %s", e)

        coro = _seed()
        try:
            task = asyncio.create_task(coro)
        except Exception:
            coro.close()  # avoid a "coroutine was never awaited" leak on failure
            raise
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
    except Exception as e:
        logger.warning("Could not schedule owner profile seed: %s", e)


def _schedule_delegation_recovery(task_agent, data_dir: str | None = None) -> None:
    """Cold-start-only sweep over autonomy_state.db:
    a delegation row still 'running' at process start was crash-interrupted. Mark
    it 'interrupted' and surface that back to its session via the self-wake rail
    (best-effort; the durable row remains the honest record when the wake drops).
    Never resumes the child. No-op unless AUTONOMY_STATE_DURABLE and the DB exists.
    Fail-open: any error is logged and swallowed, never disrupts startup.

    ``data_dir`` — the authoritative autonomy data dir start_autonomy already
    threads to the cron/goal tickers; when given it overrides the store's own
    resolution so recovery always reads the same DB the registries write.
    """
    import os

    if not AutonomyConfig.autonomy_state_durable():
        return

    async def _sweep() -> None:
        try:
            from agents.task.agent.autonomy_state import (
                default_autonomy_state_db,
                recover_interrupted_delegations,
            )
            db_path = (os.path.join(data_dir, "autonomy_state.db")
                       if data_dir else default_autonomy_state_db())
            recovered = await recover_interrupted_delegations(task_agent, db_path)
            if recovered:
                logger.info(
                    "delegation recovery: %d crash-interrupted delegation(s) "
                    "marked and surfaced", recovered)
        except Exception as e:
            logger.warning("delegation recovery sweep failed (non-fatal): %s", e)

    task = asyncio.create_task(_sweep())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _schedule_hf_deploy_reconcile() -> None:
    """Cold-start reconcile-on-boot (proposal §3.5): re-health-check every
    ``live`` hf_deploy row so a Space that died/was deleted out-of-band flips
    to ``failed`` in the registry instead of staying an honest lie. No-op
    unless ``HF_DEPLOY_ENABLED``. Fail-open: any error is logged and
    swallowed, never disrupts startup. Mirrors ``_schedule_delegation_recovery``.
    """
    if not _hf_deploy_enabled():
        return

    async def _sweep() -> None:
        try:
            from tools.hf_deploy.reconcile import reconcile_deployed_apps
            from tools.hf_deploy.registry import default_deployed_apps_db
            flipped = await reconcile_deployed_apps(db_path=default_deployed_apps_db())
            if flipped:
                logger.info(
                    "hf_deploy reconcile: flipped %d drifted live app(s) to failed", flipped)
        except Exception as e:
            logger.warning("hf_deploy reconcile sweep failed (non-fatal): %s", e)

    task = asyncio.create_task(_sweep())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


class AutonomyHandles:
    def __init__(self) -> None:
        self._entries: List[Tuple[str, asyncio.Task, asyncio.Event]] = []

    def _add(self, name: str, ticker) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(ticker.run_forever(stop_event=stop))
        self._entries.append((name, task, stop))
        logger.info("autonomy loop started: %s", name)

    async def stop(self) -> None:
        # Signal every loop to exit first, then await each so a loop that observes
        # its stop_event winds down gracefully (and runs its own cleanup). A bounded
        # timeout prevents a stubborn ticker (one that ignores stop_event, e.g. mid-job)
        # from hanging shutdown indefinitely; force-cancel after the grace window.
        for _name, _task, stop in self._entries:
            stop.set()
        for name, task, _stop in self._entries:
            try:
                await asyncio.wait_for(task, timeout=_STOP_GRACE_SEC)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            except (asyncio.CancelledError, Exception):
                pass
            logger.info("autonomy loop stopped: %s", name)
        self._entries.clear()


def start_autonomy(*, task_agent, data_dir: str | None = None) -> AutonomyHandles:
    # WS-3: an omitted data_dir resolves to the data home, never a relative "data"
    # under the cwd. Every real caller (api/app.py lifespan, the CLI surface
    # commands) passes an explicit dir, so this only closes the fallback.
    from core.runtime_paths import data_dir_or_home
    data_dir = data_dir_or_home(data_dir)
    handles = AutonomyHandles()
    try:
        # D2: self-heal the DB schema on every posture (the API lifespan already
        # migrates awaited; here it's a scheduled idempotent no-op). One-shot.
        _schedule_boot_migrations(task_agent)
    except Exception as e:
        logger.warning("Could not schedule boot migrations: %s", e)
    try:
        # G-1: seed the owner/local user_profiles row(s) once per process —
        # covers the API-lifespan + CLI-REPL entry seams. Not added to
        # `handles` (it's a one-shot sweep, not a recurring loop).
        _schedule_owner_profile_seed(task_agent)
    except Exception as e:
        logger.warning("Could not schedule owner profile seed: %s", e)
    try:
        # COLD START ONLY — see _schedule_cold_start_orphan_reap docstring. Not
        # added to `handles` (it's a one-shot sweep, not a recurring loop).
        _schedule_cold_start_orphan_reap()
    except Exception as e:
        logger.warning("Could not schedule cold-start orphan reap: %s", e)
    try:
        # One-shot recovery: delegations still 'running' in autonomy_state.db were
        # crash-interrupted — mark them and surface back to their sessions.
        _schedule_delegation_recovery(task_agent, data_dir)
    except Exception as e:
        logger.warning("Could not schedule delegation recovery: %s", e)
    try:
        # COLD START ONLY, same shape as the orphan reap above — not a recurring loop.
        _schedule_hf_deploy_reconcile()
    except Exception as e:
        logger.warning("Could not schedule hf_deploy reconcile: %s", e)
    if _cron_enabled():
        try:
            handles._add("cron", _build_cron_ticker(task_agent, data_dir))
        except Exception as e:
            logger.warning("Could not start cron ticker: %s", e)
    if _goals_enabled():
        try:
            handles._add("goals", _build_goal_ticker(task_agent, data_dir))
        except Exception as e:
            logger.warning("Could not start goal dispatcher: %s", e)
    if _curator_enabled():
        try:
            handles._add("curator", _build_curator_ticker(data_dir))
        except Exception as e:
            logger.warning("Could not start skill curator: %s", e)
    if _surface_gc_enabled():
        try:
            handles._add("surface_gc", _build_surface_gc_ticker(task_agent))
        except Exception as e:
            logger.warning("Could not start surface GC ticker: %s", e)
    if _quiet_release_enabled():
        try:
            handles._add("quiet_release", _build_quiet_release_ticker(task_agent))
        except Exception as e:
            logger.warning("Could not start quiet-hours release ticker: %s", e)
    if _x402_invoicing_enabled():
        try:
            handles._add("settlement", _build_settlement_watcher(task_agent))
        except Exception as e:
            logger.warning("Could not start x402 settlement watcher: %s", e)
    return handles
