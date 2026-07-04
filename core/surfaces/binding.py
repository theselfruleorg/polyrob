"""P1b-2: bind a session's orchestrator to the Singular Chat outbound bus.

Called from TaskAgent.create_session immediately after the orchestrator is built
and BEFORE orchestrator.initialize() — because _register_stream_callback captures
orchestrator._message_router / ._chat_session_key BY VALUE when it wires the stream
mirror, so binding after initialize() would capture None (a permanent no-op).

Flag-gated (SINGULAR_CHAT_ENABLED, default OFF) and fail-open: when OFF, or the bus
isn't installed, or no chat_session_key was supplied (legacy callers: cron/goal/
`polyrob run`/raw API), it touches NOTHING and returns False -> the orchestrator keeps
its legacy callback-only path, byte-identical to today.
"""
import logging
from typing import Any, Optional

from core.surfaces.envelopes import SessionSource

logger = logging.getLogger(__name__)


def bind_chat_surface(
    orchestrator: Any,
    container: Any,
    *,
    session_source: Optional[SessionSource],
    chat_session_key: Optional[str],
    session_id: str,
    user_id: str,
) -> bool:
    """Set the orchestrator's router+key and write the chat<->session registry row.

    Returns True when the orchestrator was bound (router+key set), False when the
    feature is off / unavailable / no key was supplied.
    """
    from agents.task.surface_config import SurfaceConfig

    if not SurfaceConfig.singular_chat_enabled():
        return False
    if container is None or not chat_session_key:
        return False
    router = container.get_service("message_router")
    if router is None:
        return False

    orchestrator._message_router = router
    orchestrator._chat_session_key = chat_session_key

    # Write the durable chat<->session row so publish can resolve key -> surface.
    # Needs the SessionSource (surface_id/chat_id); without it we still bind the
    # mirror but publish will fail-open-drop until a row exists.
    if session_source is not None:
        registry = container.get_service("session_chat_registry")
        if registry is not None:
            try:
                registry.bind(
                    chat_session_key, session_id, user_id,
                    session_source.surface_id, session_source.chat_id,
                )
            except Exception as e:  # fail-open: a bind error must not break session create
                logger.debug("bind_chat_surface registry.bind failed: %s", e)
    return True


def surface_ask_capability(orchestrator: Any) -> Optional[bool]:
    """Can the surface bound to this orchestrator collect a reply (ask)?

    Returns True/False when a surface is bound and its capability is known, or None
    when nothing is bound / the bus is absent (the legacy, surface-agnostic case).
    Used by the send_message action to avoid the wait_for_response deadlock: pausing
    a session to wait for a reply on a surface that can't deliver one would hang it.
    """
    key = getattr(orchestrator, "_chat_session_key", None)
    container = getattr(orchestrator, "container", None)
    if not key or container is None:
        return None
    try:
        registry = container.get_service("session_chat_registry")
        row = registry.resolve(key) if registry is not None else None
        if not row:
            return None
        surface_registry = container.get_service("surface_registry")
        surface = surface_registry.get(row.get("surface_id")) if surface_registry else None
        if surface is None:
            return None
        return bool(surface.capabilities.supports_interactive_ask)
    except Exception as e:  # fail-open: unknown -> legacy behavior
        logger.debug("surface_ask_capability failed: %s", e)
        return None
