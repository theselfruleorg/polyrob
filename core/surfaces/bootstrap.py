"""P1b-0: construct + register the outbound bus on a DI container.

This is the one prerequisite the rest of the Singular Chat Interface depends on:
nothing else in the codebase instantiates MessageRouter / SessionChatRegistry, so
without this call container.get_service("message_router") is always None and every
P1a outbound mirror (plus cron/delivery.py's telegram sink) is silently inert.

Gated on SINGULAR_CHAT_ENABLED so flag-OFF means the services never exist -> the
mirrors stay no-ops -> behavior is byte-identical to today. Idempotent: a second
call reuses the already-installed router so live surface subscriptions are never
dropped. Fail-open: any construction error logs and returns False rather than
breaking startup.
"""
import logging

logger = logging.getLogger(__name__)


def _ensure_conversation_store(container, db_path: str) -> None:
    """E1 (2026-07-13 review): register the durable per-correspondent conversation
    container alongside the bus. Idempotent + fail-open — the store is additive and
    every consumer treats it as optional."""
    try:
        if container.get_service("conversation_store") is not None:
            return
        import os as _os
        from core.surfaces.conversations import ConversationStore
        conv_db = _os.path.join(_os.path.dirname(db_path) or ".", "conversations.db")
        container.register_service("conversation_store", ConversationStore(conv_db))
        logger.info("surface bus: conversation store installed (%s)", conv_db)
    except Exception as e:
        logger.debug("conversation store unavailable: %s", e)


def _ensure_dead_targets(container, db_path: str):
    """T1.5 Task 4: register the dead-target registry alongside the bus.

    Built unconditionally (not gated on OUTBOUND_QUEUE_ENABLED) because
    MessageRouter's direct-send path (``attach_dead_targets``) and
    ``core/surfaces/dispatcher.py``'s revive-on-inbound lookup
    (``container.get_service("dead_targets")``) both need the store regardless
    of whether the durable outbound queue is enabled. Usage is separately gated
    by the ``DEAD_TARGET_REGISTRY`` flag at each read/write call site, so
    constructing the store here is inert (just a CREATE TABLE) when the flag is
    off. Idempotent + fail-open, mirrors ``_ensure_conversation_store``.
    Returns the store (existing or newly built), or None on construction
    failure.
    """
    try:
        existing = container.get_service("dead_targets")
        if existing is not None:
            return existing
        import os as _os
        from core.surfaces.dead_targets import DeadTargetStore
        dt_db = _os.path.join(_os.path.dirname(db_path) or ".", "dead_targets.db")
        dt = DeadTargetStore(dt_db)
        container.register_service("dead_targets", dt)
        logger.info("surface bus: dead-target registry installed (%s)", dt_db)
        return dt
    except Exception as e:
        logger.debug("dead-target registry unavailable: %s", e)
        return None


def _ensure_correspondent_registry(container, db_path: str) -> None:
    """030 WS-B2 (finding L3): the correspondent registry was registered only by
    the email seat, so with CORRESPONDENT_ACCESS_ENABLED on a telegram-only
    deploy every third party resolved DENIED (access.py gets registry=None).
    Register it centrally, on every seat that installs the surface bus, gated
    by the same flag the access model reads. Fail-open."""
    try:
        from core.surfaces.config import SurfaceConfig
        if not SurfaceConfig.correspondent_access_enabled():
            return
        if container.get_service("correspondent_registry") is not None:
            return
        import os as _os
        from core.surfaces.correspondents import CorrespondentRegistry
        data_dir = _os.path.dirname(db_path) or "."
        container.register_service(
            "correspondent_registry",
            CorrespondentRegistry(_os.path.join(data_dir, "correspondents.db")),
        )
        logger.info("surface bus: correspondent registry installed (central, WS-B2)")
    except Exception as e:
        logger.error("correspondent registry install failed: %s", e)


def install_surface_bus(container, db_path: str = None) -> bool:
    """Build SessionChatRegistry + MessageRouter and register them on ``container``.

    Returns True if the bus is present on the container after the call (installed
    now or already present), False if the flag is OFF or construction failed.

    ``db_path`` defaults to ``<container.config.data_dir>/surfaces.db`` so the bus,
    outbox, and circuit store follow POLYROB_DATA_DIR isolation instead of a hardcoded
    ``./data`` inside the code tree. Pass an explicit path to override.
    """
    from core.surfaces.config import SurfaceConfig

    if not SurfaceConfig.singular_chat_enabled():
        return False

    if db_path is None:
        import os as _os
        from core.runtime_paths import data_dir_or_home
        data_dir = data_dir_or_home(getattr(getattr(container, "config", None), "data_dir", None))
        db_path = _os.path.join(data_dir, "surfaces.db")

    # Idempotent: never clobber a live router (it holds surface subscriptions).
    existing = container.get_service("message_router")
    if existing is not None:
        _ensure_conversation_store(container, db_path)
        _ensure_correspondent_registry(container, db_path)
        dt = _ensure_dead_targets(container, db_path)
        if dt is not None:
            existing.attach_dead_targets(dt)
        return True

    try:
        from core.surfaces.session_chat_registry import SessionChatRegistry
        from core.surfaces.message_router import MessageRouter

        registry = SessionChatRegistry(db_path)
        router = MessageRouter(registry)
        container.register_service("session_chat_registry", registry)
        container.register_service("message_router", router)

        from core.surfaces.outbound_allowlist import OutboundAllowlist
        container.register_service("outbound_allowlist", OutboundAllowlist(db_path))

        _ensure_conversation_store(container, db_path)
        _ensure_correspondent_registry(container, db_path)

        dt = _ensure_dead_targets(container, db_path)
        if dt is not None:
            router.attach_dead_targets(dt)

        # 2026-08-30: construction is deliberately UNCONDITIONAL now — decoupled
        # from OUTBOUND_QUEUE_ENABLED, which stays the sole gate for publish()'s
        # PRIMARY reply-routing decision (direct-send vs. queued-with-retry for a
        # locally-subscribed surface, e.g. the main telegram conversation) at
        # message_router.py's `publish()`. Before this, a surface not hosted in
        # THIS process (e.g. `message(surface="email")` called from the agent
        # daemon, which only polyrob-email.service subscribes locally) got an
        # unconditional "no surface X registered — delivery failed", because
        # send_message()'s cross-process fallback required BOTH self._queue and
        # the flag — so with the flag off (the prod default) that fallback could
        # never exist, live-observed 2+ times/day burning retries on a treasury
        # goal (2026-08-28 21:26Z/22:17Z, re-observed 2026-08-30
        # 00:16Z/00:22Z). Always building the queue+dispatcher+circuit-breaker
        # makes that fallback usable regardless of the flag; publish()'s own
        # `SurfaceConfig.outbound_queue_enabled()` check is untouched, so the
        # primary reply-routing behavior stays byte-identical to before.
        import os
        from core.surfaces.outbound_queue import OutboundDeliveryQueue
        from core.surfaces.outbound_dispatcher import OutboundDispatcher
        from core.surfaces.circuit import CircuitStore, SurfaceCircuitBreaker
        q = OutboundDeliveryQueue(os.path.join(os.path.dirname(db_path) or ".", "outbox.db"))
        q.reclaim_inflight(older_than=__import__("time").time() - 120)  # restart-recovery
        router.attach_queue(q)
        circuit_store = CircuitStore(
            os.path.join(os.path.dirname(db_path) or ".", "surface_state.db")
        )
        circuit = SurfaceCircuitBreaker(store=circuit_store)
        # event_log is deliberately left unwired here: no core-tier handle to
        # core.event_log exists on this path, and adding one
        # would require a new core/surfaces/bootstrap.py -> agents.task.telemetry
        # .event_log edge to tests/test_layering_ratchet.py's frozen allowlist,
        # which may only shrink (never grow). The dispatcher's event_log param
        # defaults to None and its emit helper is already a no-op in that case
        # (see OutboundDispatcher._emit_dead_target_event) — dead-target skip/mark
        # still logs at INFO either way, only the telemetry event is absent.
        dispatcher = OutboundDispatcher(q, lambda sid: router._surfaces.get(sid),
                                        circuit=circuit, dead_targets=dt)
        container.register_service("outbound_queue", q)
        container.register_service("outbound_dispatcher", dispatcher)
        container.register_service("surface_circuit_breaker", circuit)
        logger.info("surface bus: durable outbound queue + dispatcher + circuit breaker constructed")
        logger.info("surface bus installed (session_chat_registry + message_router)")
        return True
    except Exception as e:  # fail-open: a bus build error must not break startup
        logger.error("install_surface_bus failed: %s", e, exc_info=True)
        return False
