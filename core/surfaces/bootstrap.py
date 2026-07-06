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


def install_surface_bus(container, db_path: str = None) -> bool:
    """Build SessionChatRegistry + MessageRouter and register them on ``container``.

    Returns True if the bus is present on the container after the call (installed
    now or already present), False if the flag is OFF or construction failed.

    ``db_path`` defaults to ``<container.config.data_dir>/surfaces.db`` so the bus,
    outbox, and circuit store follow POLYROB_DATA_DIR isolation instead of a hardcoded
    ``./data`` inside the code tree. Pass an explicit path to override.
    """
    from agents.task.surface_config import SurfaceConfig

    if not SurfaceConfig.singular_chat_enabled():
        return False

    # Idempotent: never clobber a live router (it holds surface subscriptions).
    existing = container.get_service("message_router")
    if existing is not None:
        return True

    if db_path is None:
        import os as _os
        data_dir = getattr(getattr(container, "config", None), "data_dir", "data") or "data"
        db_path = _os.path.join(data_dir, "surfaces.db")

    try:
        from core.surfaces.session_chat_registry import SessionChatRegistry
        from core.surfaces.message_router import MessageRouter

        registry = SessionChatRegistry(db_path)
        router = MessageRouter(registry)
        container.register_service("session_chat_registry", registry)
        container.register_service("message_router", router)

        from core.surfaces.outbound_allowlist import OutboundAllowlist
        container.register_service("outbound_allowlist", OutboundAllowlist(db_path))

        if SurfaceConfig.outbound_queue_enabled():
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
            dispatcher = OutboundDispatcher(q, lambda sid: router._surfaces.get(sid),
                                            circuit=circuit)
            container.register_service("outbound_queue", q)
            container.register_service("outbound_dispatcher", dispatcher)
            container.register_service("surface_circuit_breaker", circuit)
            logger.info("surface bus: durable outbound queue + dispatcher + circuit breaker constructed")
        logger.info("surface bus installed (session_chat_registry + message_router)")
        return True
    except Exception as e:  # fail-open: a bus build error must not break startup
        logger.error("install_surface_bus failed: %s", e, exc_info=True)
        return False
