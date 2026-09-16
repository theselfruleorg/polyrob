"""Start/stop the autonomy background loops (cron, goals, curator) for ANY entry.

Previously these tickers were inlined in api/app.py's lifespan, so the terminal
(the terminal agent) never ran them. This module is the single shared place both the FastAPI
server and the CLI REPL call. Each loop is independently gated and fail-open: one
loop failing to build never blocks the others. Idempotent stop()."""
from __future__ import annotations

import asyncio
import logging
from core.config_policy import AutonomyConfig, _mode_capability_default, autonomy_enabled
from typing import Any, Dict, List, Tuple

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
    from core.surfaces.config import SurfaceConfig
    return SurfaceConfig.surface_gc_enabled()


def _sandbox_reap_enabled() -> bool:
    """043 A8/A42: `sandbox_reap` was the only always-on loop with no flag and
    no pause kind — this + the ``allows("sandbox_reap")`` check in its tick
    close both gaps. Default ON: reach only, no behavior change for an
    install that never touches the flag."""
    from core.env import bool_env
    return bool_env("SANDBOX_REAP_ENABLED", True)


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

#: How often to sweep sandbox containers whose owning session is gone
#: (``_build_sandbox_reaper_ticker``). Ownership-keyed, so this is safe to run
#: periodically — unlike the age-based cold-start ``reap_orphans``.
_SANDBOX_REAP_INTERVAL_SEC = 900

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


def _wake_drain_enabled() -> bool:
    """043 W10: the cross-process wake-drain loop only makes sense where the
    self-wake rail is on — ``deliver_self_wake`` no-ops otherwise, so a drain
    would just churn rows to ``dropped``. Gated on the SAME process-level
    env/posture default ``deliver_self_wake`` reads
    (``AutonomyConfig.self_wake_enabled``), so a default install (self-wake OFF)
    starts no drain and leaves the loop set the autonomy tests assert unchanged.
    When ON, the OWNING process drains the rows a SEPARATE console process
    enqueued on an owner approval."""
    return AutonomyConfig.self_wake_enabled()


def _build_wake_drain_ticker(task_agent, data_dir):
    """043 W10: drain durable cross-process session-wake rows (a console approval
    in a SEPARATE process wrote them) and deliver each through THIS process's own
    self-wake rail — ``deliver_self_wake`` refuses a remote session, so the wake
    must run where the session lives. Single-winner CAS per row (safe if two
    processes both drain); the 031 pause is honored inside ``deliver_self_wake``
    (a held wake is retried, then dropped at the attempt cap — the grant is
    durable, so a lost nudge is safe). Fail-open: a tick never disrupts the
    runtime."""
    import os

    from core.tickers import IntervalTicker
    from core.wake_queue import WAKE_DRAIN_INTERVAL_SEC, get_wake_queue

    def _queue():
        db_path = os.path.join(data_dir, "wakes.db") if data_dir else None
        return get_wake_queue(db_path)

    async def _tick():
        try:
            owner = f"pid:{os.getpid()}"
            queue = _queue()
            for row in queue.claim_pending(owner):
                # I1 (043 W10): under opt-in SESSION_REGISTRY_BACKEND=sqlite +
                # workers>1, a NON-owning worker that wins the CAS here could
                # recreate an evicted session in the wrong worker — we do NOT
                # re-gate the drain on `route_session().is_local`. That is safe in
                # the default single-agent topology (one process runs
                # start_autonomy) and acceptable under the opt-in one: the drain
                # trusts `deliver_self_wake`'s own resident-or-recreatable check
                # (it drops + audits a session it cannot legitimately own), and a
                # wrongly-recreated session's forged wake still cannot spend.
                try:
                    ok = await task_agent.deliver_self_wake(
                        row.session_id, row.user_id, row.text,
                        metadata=row.metadata)
                except Exception:
                    logger.debug("wake drain: deliver raised for %s (fail-open)",
                                 row.session_id, exc_info=True)
                    ok = False
                if ok:
                    queue.mark_delivered(row.id)
                else:
                    queue.fail(row.id)
        except Exception as e:
            logger.debug("wake drain tick failed: %s", e)

    return IntervalTicker(_tick, interval_seconds=WAKE_DRAIN_INTERVAL_SEC)


def _build_surface_gc_ticker(task_agent):
    """a5: periodically purge stale chat<->session bindings so the routing map can't grow
    unboundedly. Resolves the registry from the task_agent's container (no extra
    plumbing through start_autonomy); fail-open and no-op when the chat bus is off."""
    from core.tickers import IntervalTicker
    from core.surfaces.config import SurfaceConfig

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


def _live_session_ids(task_agent):
    """Session ids currently resident, or ``None`` when that cannot be read.

    ``None`` and ``[]`` mean different things here and the difference is
    destructive: ``DockerBackend.reap_unowned`` treats an empty live set as
    "every session is gone" and force-removes every labeled container past its
    race guard. So an unreadable registry must be UNKNOWN (``None`` → the tick
    skips), and only a registry that actually answered "nothing is resident"
    may be ``[]``.

    Duck-typed rather than imported: ``core/`` may not import ``agents.*``
    (tests/test_layering_ratchet.py), and this needs no more than "does the
    object expose a registry that can list session ids". The agents-tier
    ``session_registry.resident_session_ids`` stays for callers inside that
    tier — it collapses the two cases to ``[]``, which is safe there and is not
    safe here.
    """
    registry = getattr(task_agent, "_registry", None)
    lister = getattr(registry, "session_ids", None)
    if not callable(lister):
        return None
    try:
        return list(lister())
    except Exception:
        return None


def _build_sandbox_reaper_ticker(task_agent):
    """Periodically remove sandbox containers whose owning session is GONE.

    ⚠️ Read ``_schedule_cold_start_orphan_reap`` first: it documents, correctly,
    that the AGE-based ``reap_orphans`` must never be periodic, because an idle
    chat session legitimately outlives any sane max age. This ticker does not
    contradict that rule — it keys on OWNERSHIP, not age. A container is removed
    only when its ``polyrob.session`` label names a session that is no longer
    resident in the SessionRegistry, so an idle-but-live session is never
    touched.

    Why it exists: only the full ``SessionCleanupMixin.cleanup`` and the one-shot
    autonomous ``run_as_session`` path release a container. A resident chat
    session gets the PARTIAL cleanup (deliberately keeping its container for
    continuous chat), so once its orchestrator is evicted the container is simply
    abandoned until the next process restart. Live on prod 2026-08-24: 19
    containers, oldest 2 days, disk at 80%.

    No-op unless persistent docker sandboxes are enabled AND the docker CLI is
    present. Fail-open: a tick must never disrupt the runtime.
    """
    from core.tickers import IntervalTicker

    async def _tick():
        try:
            from core.autonomy_control import allows
            if not allows("sandbox_reap").allowed:
                return  # 031 owner pause

            import shutil
            from tools.code_exec import code_exec_docker_persistent_enabled

            if not code_exec_docker_persistent_enabled():
                return
            if shutil.which("docker") is None:
                return

            # Resolve the live session set. If we cannot read the registry we must
            # NOT sweep — an empty "live" set would look like "every session is
            # gone" and reap every container. Bail out instead.
            live = _live_session_ids(task_agent)
            if live is None:
                logger.debug("sandbox reap: session registry unavailable; skipping tick")
                return

            from tools.code_exec.backends.docker import DockerBackend
            removed = await DockerBackend.reap_unowned(live)
            if removed:
                logger.info(
                    "sandbox reap: removed %d container(s) whose session is gone", removed
                )
        except Exception as e:  # a reap tick must never disrupt the runtime
            logger.debug("sandbox reap tick failed: %s", e)

    return IntervalTicker(_tick, interval_seconds=_SANDBOX_REAP_INTERVAL_SEC)


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
        n = _requeue_on_boot(GoalBoard(_os.path.join(data_dir, "goals.db")))
        if n:
            logger.info("cold-start goal sweep: re-queued %d running goal(s)", n)
    except Exception:
        logger.debug("cold-start goal sweep skipped", exc_info=True)
    return build_goal_ticker(task_agent, data_dir=data_dir)


def _requeue_on_boot(board) -> int:
    """§5.1 cold-start requeue. NOT pause-gated (031 review): a `running` row after
    a restart is a lie whatever the pause state, and requeueing starts nothing —
    dispatch itself is gated. Leaving the rows `running` would let the unconditional
    `reclaim_stale` count the restart as a failure once the claim TTL lapsed."""
    return board.requeue_running_on_boot()


def _build_curator_ticker(data_dir):
    from agents.task.agent.core.curator import build_curator_ticker
    return build_curator_ticker(data_dir=data_dir)


def _build_bridge_watcher(task_agent):
    """039 Unit C — reconcile in-flight bridges and refresh the balance cache.

    ⚠️ NOT pause-gated, and that is deliberate. Every other starter here begins
    WORK; this one reads balances and reports what it finds. An owner who has just
    stopped everything is exactly the owner who needs to know where their
    in-flight funds are, and a stop button that also switched off the answer would
    be one nobody dares press. See the module docstring.
    """
    from core.tickers import IntervalTicker
    from core.wallet import bridge_watcher

    container = getattr(task_agent, "container", None)

    async def _tick():
        try:
            result = await bridge_watcher.tick(container)
            if result.arrived or result.failed or result.escalated:
                logger.info("bridge watcher: %s", result.as_dict())
        except Exception as e:
            logger.warning("bridge watcher tick failed: %s", e)
        try:
            await asyncio.to_thread(bridge_watcher.refresh_balances, container)
        except Exception as e:
            logger.debug("bridge watcher: balance refresh failed: %s", e)

    return IntervalTicker(_tick, interval_seconds=bridge_watcher.INTERVAL_SEC)


def _build_settlement_watcher(task_agent):
    # Lazy server-tier import — only executes when X402_INVOICE_ENABLED is on,
    # so a rob-core-only environment never touches modules.x402.
    from modules.x402.settlement_watcher import build_settlement_watcher
    watcher = build_settlement_watcher(task_agent)
    # 046 T1: the room-action branch of `_notify` needs a CONTAINER to reach the
    # offer store and the chat transport. The seam declared itself "assigned
    # after construction by whoever wires the watcher" and nobody did, so every
    # settled paid action was credited instead of applied. This is that wiring;
    # `_room_moderator` stays None and is resolved from the container service,
    # which is also what keeps it injectable in a test.
    try:
        watcher._room_container = getattr(task_agent, "container", None)
    except Exception as e:      # pragma: no cover - an exotic watcher shape
        logger.debug("settlement watcher: room container not attached (%s)", e)
    return watcher


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
            except ImportError as e:
                logger.error(
                    "migrations package missing — schema cannot migrate "
                    "(broken install: reinstall polyrob): %s", e)
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


_self_binding_sweep_scheduled = False


def _schedule_self_binding_sweep(task_agent) -> None:
    """031 T15 cold-start sweep: expire any correspondent binding to the agent's
    OWN email address. One such binding re-ran a finished treasury goal on every
    restart (the agent's sent copy routed back in as correspondent DATA). Fail-open,
    once per process; no-op without a correspondent registry."""
    global _self_binding_sweep_scheduled
    if _self_binding_sweep_scheduled:
        return
    _self_binding_sweep_scheduled = True

    async def _sweep() -> None:
        try:
            from core.surfaces.seed import is_self_address
            container = getattr(task_agent, "container", None)
            registry = container.get_service("correspondent_registry") if container else None
            if registry is None or not hasattr(registry, "deactivate"):
                return
            n = 0
            for r in registry.list() or []:
                if (r.get("surface") == "email" and r.get("state") == "active"
                        and is_self_address(r.get("address") or "")):
                    if registry.deactivate(surface=r["surface"], address=r["address"],
                                           thread_id=r.get("thread_id") or None,
                                           user_id=r.get("user_id")):
                        n += 1
            if n:
                logger.warning("correspondent_self_binding_removed: %d binding(s) to the "
                               "agent's own address expired", n)
        except Exception as e:
            logger.warning("self-binding sweep failed (non-fatal): %s", e)

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

    if not AutonomyConfig.autonomy_state_durable():
        return

    async def _sweep() -> None:
        try:
            # 031: NOT pause-gated — marking a crash-interrupted row `interrupted`
            # is the honest record either way; the self-wake it surfaces through is
            # itself pause-gated (dropped with reason "paused"), and this sweep
            # never runs again after boot.
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
        #: Resolved AUTONOMY_ENABLED master at start (0.9.0). Query point for the
        #: awareness surfaces; the per-flag gates are the authoritative loop gate.
        self.autonomy_enabled: bool = True
        self._hb_task: "asyncio.Task | None" = None
        self._hb_stop: "asyncio.Event | None" = None
        #: 031: name -> ticker, so the pause transition can reach the goal
        #: dispatcher / cron scheduler that own the in-flight work.
        self._loops: Dict[str, Any] = {}
        self._task_agent: Any = None
        #: 046: the data home the heartbeat's room-action expiry sweep reads.
        #: None until `start_autonomy` sets it; the sweep then no-ops.
        self._data_dir: "str | None" = None
        self._watch_task: "asyncio.Task | None" = None
        self._watch_stop: "asyncio.Event | None" = None

    def _add(self, name: str, ticker) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(ticker.run_forever(stop_event=stop))
        self._entries.append((name, task, stop))
        self._loops[name] = ticker
        logger.info("autonomy loop started: %s", name)
        self._ensure_heartbeat()

    @property
    def loops(self) -> Dict[str, Any]:
        return dict(self._loops)

    # --- 031 owner pause: ONE reconcile path for this process --------------------
    # Two triggers, one body: the in-process transition hook (immediate, when
    # THIS process wrote the record) and the pause watcher (a cheap poll of the
    # record, so a pause written by ANOTHER process — the CLI, the web console,
    # a script, a touched file — is honoured within seconds, not on the next
    # 60 s tick). `_reconcile_pause` is idempotent: holds/cancels are CAS-shaped.
    _PAUSE_WATCH_SEC = 3.0

    def on_pause_transition(self, old, new) -> None:
        """Registered with ``core.autonomy_control.register_transition_hook``."""
        self._maybe_reconcile(old, new)

    def _maybe_reconcile(self, old, new) -> None:
        widened = new.paused and (not old.paused or set(new.scopes) != set(old.scopes))
        if not widened:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # a CLI process without a loop: nothing of ours is running
        task = loop.create_task(self._reconcile_pause(new))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    async def _pause_watch_loop(self, data_dir) -> None:
        from core.autonomy_control import read_state
        last = read_state(data_dir)
        while self._watch_stop is not None and not self._watch_stop.is_set():
            try:
                await asyncio.wait_for(self._watch_stop.wait(), timeout=self._PAUSE_WATCH_SEC)
                break
            except asyncio.TimeoutError:
                pass
            try:
                cur = read_state(data_dir)
            except Exception:
                continue
            if cur.paused != last.paused or set(cur.scopes) != set(last.scopes):
                logger.info("pause watcher: %s -> %s", "paused" if last.paused else "running",
                            f"paused ({', '.join(cur.scopes)})" if cur.paused else "running")
                self._maybe_reconcile(last, cur)
                last = cur

    def _start_pause_watcher(self, data_dir) -> None:
        if self._watch_task is not None:
            return
        try:
            self._watch_stop = asyncio.Event()
            self._watch_task = asyncio.create_task(self._pause_watch_loop(data_dir))
        except Exception as e:  # never let the watcher block startup
            logger.warning("pause watcher not started: %s", e)

    async def _reconcile_pause(self, state) -> None:
        from core.autonomy_control import allows
        scopes = ", ".join(state.scopes)
        goals = self._loops.get("goals")
        if goals is not None and not allows("dispatch").allowed:
            try:
                goals.dispatcher._paused_seen = True  # before the await: no double hold
                held = await goals.dispatcher.hold_inflight(f"owner pause ({scopes})")
                if held:
                    logger.warning("pause: held %d in-flight goal run(s)", len(held))
            except Exception:
                logger.warning("pause: goal hold failed", exc_info=True)
        cron = self._loops.get("cron")
        if cron is not None:
            try:
                if cron.scheduler.cancel_inflight():
                    logger.warning("pause: cancelled the in-flight cron run")
            except Exception:
                logger.warning("pause: cron cancel failed", exc_info=True)
        if self._task_agent is not None and not allows("dispatch").allowed:
            try:
                # Duck-typed (core may not import agents.*): the TaskAgent cancels
                # its autonomous sessions' background delegations, never the owner's.
                cancel = getattr(self._task_agent, "cancel_autonomous_delegations", None)
                n = int(cancel("owner pause") or 0) if callable(cancel) else 0
                if n:
                    logger.warning("pause: cancelled %d background delegation(s)", n)
            except Exception:
                logger.warning("pause: delegation cancel failed", exc_info=True)

    # --- liveness heartbeat (2026-08-28, status SSOT D13) ------------------
    # core/tickers.py::TickerSupervisor has emitted an `autonomy_tick` per loop
    # since the 2026-07-04 audit — but the API lifespan, the REPL and the
    # Telegram surface all start their loops through THIS class, which never
    # did. Prod had 16k telemetry rows and zero heartbeats, so a dead cron/goal
    # task rendered identically to a live one. Same emitter, same event.
    def _ensure_heartbeat(self) -> None:
        if self._hb_task is not None:
            return
        try:
            self._hb_stop = asyncio.Event()
            self._hb_task = asyncio.create_task(self._heartbeat_loop())
        except Exception as e:  # never let liveness reporting block startup
            logger.warning("autonomy heartbeat not started: %s", e)

    def emit_heartbeats(self) -> None:
        """Record one ``autonomy_tick`` per loop (alive = task not done). Fail-open."""
        from core.tickers import emit_loop_heartbeats
        emit_loop_heartbeats([(name, task) for name, task, _stop in self._entries],
                             source="autonomy_runtime")

    async def _heartbeat_loop(self) -> None:
        from core.tickers import _heartbeat_interval_sec
        interval = _heartbeat_interval_sec()
        self.emit_heartbeats()  # first beat immediately: the status surfaces read it
        while self._hb_stop is not None and not self._hb_stop.is_set():
            try:
                await asyncio.wait_for(self._hb_stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._hb_stop is not None and self._hb_stop.is_set():
                break
            self.emit_heartbeats()
            self._expire_room_action_offers()

    def _expire_room_action_offers(self) -> None:
        """046: an UNPAID offer stops inviting payment once its room's TTL
        passes — a payer who sends late would pay for something nothing will
        apply.

        Rides the heartbeat because it is a pure local sweep: no network, no
        model call, and it must run in every posture. Deliberately NOT
        pause-gated — expiring an offer is protective, and a paused instance
        that kept inviting payment would be the worse failure. Fail-open: a
        sweep fault must never stop the heartbeat.
        """
        try:
            import os as _os

            from core.surfaces.chat_policy import load_for_chat
            from core.surfaces.room_action_store import OfferStore, store_path
            from core.surfaces.room_actions import parse_duration
            if not self._data_dir:
                return
            path = store_path(self._data_dir)
            if not _os.path.exists(path):
                return
            store = OfferStore(path)
            expired = 0
            # ⚠️ Per ROOM, not one global cutoff: `chat.paid_offer_ttl` is the
            # room's own setting, and sweeping everything on a single TTL would
            # silently override it.
            for surface, chat_id in store.pending_rooms():
                policy = load_for_chat(self._data_dir, surface, chat_id)
                ttl = parse_duration(policy.paid_offer_ttl) or 1800
                expired += store.expire_stale(ttl, surface=surface,
                                              chat_id=chat_id)
            if expired:
                logger.info("room actions: expired %d unpaid offer(s)", expired)
        except Exception as e:
            logger.debug("room-action expiry sweep failed (%s)", e)

    async def stop(self) -> None:
        try:
            from core.autonomy_control import unregister_transition_hook
            unregister_transition_hook(self.on_pause_transition)
        except Exception:
            logger.debug("pause hook unregister failed", exc_info=True)
        if self._watch_stop is not None:
            self._watch_stop.set()
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watch_task = None
        if self._hb_stop is not None:
            self._hb_stop.set()
        if self._hb_task is not None:
            self._hb_task.cancel()
            try:
                await self._hb_task
            except (asyncio.CancelledError, Exception):
                pass
            self._hb_task = None
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


#: The characters a role string may contain. A role is stamped into a telemetry
#: row an owner reads, so it is bounded rather than free argv text.
_ROLE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")

#: ⚠️ The `polyrob` GROUP options that take a VALUE (`cli/polyrob.py::cli`).
#: Their argument is the next token, and it is NOT the subcommand: without this
#: `polyrob -P work telegram` reported the role `work` — a profile name where
#: the field promises a process role. `--opt=value` needs no entry here (it is
#: one token). Flags (`--plain`, `-V/--version`, `--help`) take no value.
_VALUE_OPTIONS = frozenset({
    "--project", "--model", "-m", "--provider", "-p", "--toolset",
    "--profile", "-P",
})


def _service_role() -> str:
    """Which PROCESS this is: ``telegram`` / ``email`` / ``api`` / ``console`` / ``repl`` / …

    Derived from ``sys.argv`` ALONE, deliberately. A role is an observation
    about the running process, not configuration: an env var would be one more
    thing an operator could set to something untrue, and every env read in the
    shipped tree owes a `docs/CONFIGURATION.md` row (`test_flags_reverse`) —
    a catalog row for a value nobody should ever set is a lie in the catalog.

    Descriptive only. Nothing is gated on it; the status snapshot groups rows by
    ``pid``. Unknown is said out loud rather than guessed.

    The two non-CLI entry points are named from argv[0], because neither has a
    click subcommand to read: ``python main.py`` is the API service (``api``),
    and ``python -m uvicorn <app>`` is ``console`` when it serves a
    ``webview.*`` app (the units at `deployment/polyrob-webview.service` and
    `polyrob-webgate.service`) else ``api``. They are kept DISTINCT because they
    are distinct processes with distinct loop sets; collapsing the console into
    ``api`` would put one name on two things an owner has to tell apart.
    """
    try:
        import os
        import sys
        argv = list(sys.argv or [])
        if not argv:
            return "unknown"
        prog = str(argv[0] or "")
        rest = [str(a or "") for a in argv[1:]]
        # uvicorn is reached BOTH as `python -m uvicorn` (argv[0] is the
        # package's `__main__.py`, so the basename is not "uvicorn") and as the
        # console script, so the whole path is what is searched.
        if "uvicorn" in prog.lower():
            for token in rest:
                if token.startswith("-"):
                    continue
                return "console" if token.lower().startswith("webview") else "api"
            return "api"
        skip_next = False
        for raw in rest:
            token = raw.strip().lower()
            if skip_next:
                skip_next = False
                continue
            if not token:
                continue
            if token.startswith("-"):
                # A group option's VALUE is the next token — skip both, or the
                # value is read as the subcommand.
                skip_next = "=" not in token and token in _VALUE_OPTIONS
                continue
            # The click subcommand, if it looks like one. Free text (a `polyrob
            # run "…"` task) is reported unknown rather than quoted back.
            if (len(token) <= 24 and token[0].isascii() and token[0].isalpha()
                    and all(c in _ROLE_CHARS for c in token)):
                return token
            return "unknown"
        base = os.path.basename(prog).lower()
        if base.startswith("polyrob") or base == "rob":
            return "repl"          # a bare `polyrob` opens the chat REPL
        if base == "main.py":
            return "api"
        return "unknown"
    except Exception:
        return "unknown"


def start_autonomy(*, task_agent, data_dir: str | None = None) -> AutonomyHandles:
    # WS-3: an omitted data_dir resolves to the data home, never a relative "data"
    # under the cwd. Every real caller (api/app.py lifespan, the CLI surface
    # commands) passes an explicit dir, so this only closes the fallback.
    from core.runtime_paths import data_dir_or_home
    data_dir = data_dir_or_home(data_dir)
    handles = AutonomyHandles()
    handles._task_agent = task_agent
    handles._data_dir = data_dir

    # 031: the owner pause record. Loops always start ARMED (a paused deployment
    # keeps its tickers so a resume takes effect without a restart); each tick
    # consults allows(). This process reconciles its own in-flight work on the
    # paused edge through the transition hook.
    try:
        from core.autonomy_control import read_state, register_transition_hook
        register_transition_hook(handles.on_pause_transition)
        handles._start_pause_watcher(data_dir)
        _st = read_state(data_dir)
        if _st.paused:
            logger.warning(
                "autonomy is PAUSED (%s, by %s via %s) — loops start ARMED and idle; "
                "resume takes effect without a restart",
                ", ".join(_st.scopes), _st.set_by or _st.source, _st.via or "-")
    except Exception:
        logger.warning("autonomy pause probe failed at start (non-fatal)", exc_info=True)

    # 0.9.0 legibility: the AUTONOMY_ENABLED master governs the self-directed loop
    # DEFAULTS (via T1's per-flag gates). Record + log it as the single query point
    # for the awareness surfaces. NOTE: we deliberately do NOT return early here —
    # start_autonomy also runs one-shot recovery sweeps (owner-profile seed, boot
    # migrations, delegation/orphan recovery) and independently-gated NON-autonomy
    # loops (x402 settlement watcher, surface GC, quiet-release), and it must still
    # honor an explicit per-loop opt-in (e.g. CRON_ENABLED=true) even when the master
    # is off. The per-flag gates below (_cron_enabled/_goals_enabled/_curator_enabled,
    # already False under T1 when autonomy is off) are the authoritative gate; this is
    # the visible signal, not a second mechanism.
    try:
        handles.autonomy_enabled = autonomy_enabled()
    except Exception:
        handles.autonomy_enabled = True  # fail-open: never block startup on this probe
    if not handles.autonomy_enabled:
        logger.info(
            "autonomy disabled (AUTONOMY_ENABLED off) — self-directed loops "
            "(cron/goal/curator) will not start unless individually enabled")

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
        # 031 T15: expire any correspondent binding to the agent's own address.
        _schedule_self_binding_sweep(task_agent)
    except Exception as e:
        logger.warning("Could not schedule self-binding sweep: %s", e)
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
    if _sandbox_reap_enabled():
        try:
            # Ownership-keyed sandbox container sweep. Safe to run periodically
            # (see the builder's docstring for why this does not violate the
            # cold-start-only rule that governs the AGE-based reap_orphans).
            handles._add("sandbox_reap", _build_sandbox_reaper_ticker(task_agent))
        except Exception as e:
            logger.warning("Could not start sandbox reaper ticker: %s", e)
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
    if _wake_drain_enabled():
        try:
            # 043 W10: deliver durable cross-process session wakes a console
            # approval (a separate service) enqueued for a session THIS process
            # owns.
            handles._add("wake_drain", _build_wake_drain_ticker(task_agent, data_dir))
        except Exception as e:
            logger.warning("Could not start wake-drain ticker: %s", e)
    if _x402_invoicing_enabled():
        try:
            handles._add("settlement", _build_settlement_watcher(task_agent))
        except Exception as e:
            logger.warning("Could not start x402 settlement watcher: %s", e)
    try:
        from core.wallet import bridge_watcher as _bw
        if _bw.enabled():
            handles._add("bridges", _build_bridge_watcher(task_agent))
    except Exception as e:
        logger.warning("Could not start the bridge watcher: %s", e)
    # 043 A8/A42: the durable "what did this process actually start" record —
    # the status snapshot's loop-liveness check reads it as its `expected` set
    # instead of hardcoding cron/goals.
    #
    # 043 T3: stamped with the PID (and a descriptive role) because the reader
    # used to take the newest row from ANY process. On a box where the owner
    # opens a `rob` REPL beside the service — exactly what local mode is for —
    # the REPL's row is newer and names ITS loops, silently redefining what the
    # SERVICE is expected to be running. The pid is what lets the reader union
    # the live processes instead of trusting whoever wrote last.
    #
    # Fail-open: a telemetry write must never affect startup.
    try:
        import os

        from core.event_kinds import AUTONOMY_STARTED
        from core.event_log import get_event_log
        get_event_log().record(AUTONOMY_STARTED, user_id="", source="runtime",
                               attrs={"loops": sorted(handles._loops.keys()),
                                      "pid": os.getpid(),
                                      "role": _service_role()})
    except Exception:
        logger.debug("autonomy_started record failed (non-fatal)", exc_info=True)
    return handles
