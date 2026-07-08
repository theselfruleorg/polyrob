"""Start/stop the autonomy background loops (cron, goals, curator) for ANY entry.

Previously these tickers were inlined in api/app.py's lifespan, so the terminal
(`rob`) never ran them. This module is the single shared place both the FastAPI
server and the CLI REPL call. Each loop is independently gated and fail-open: one
loop failing to build never blocks the others. Idempotent stop()."""
from __future__ import annotations

import asyncio
import logging
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
    from agents.task.constants import AutonomyConfig
    return AutonomyConfig.goals_enabled()


def _curator_enabled() -> bool:
    from agents.task.constants import AutonomyConfig
    return AutonomyConfig.curator_enabled()


def _surface_gc_enabled() -> bool:
    from agents.task.surface_config import SurfaceConfig
    return SurfaceConfig.surface_gc_enabled()


def _x402_invoicing_enabled() -> bool:
    # Read the env directly (core.env SSOT) — importing modules.x402 here would
    # put a server-tier module on the core import graph (C3 boundary).
    from core.env import bool_env
    return bool_env("X402_INVOICE_ENABLED", False)


_SURFACE_GC_INTERVAL_SEC = 3600  # hourly


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

    from agents.task.constants import AutonomyConfig

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


def start_autonomy(*, task_agent, data_dir: str = "data") -> AutonomyHandles:
    handles = AutonomyHandles()
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
    if _x402_invoicing_enabled():
        try:
            handles._add("settlement", _build_settlement_watcher(task_agent))
        except Exception as e:
            logger.warning("Could not start x402 settlement watcher: %s", e)
    return handles
