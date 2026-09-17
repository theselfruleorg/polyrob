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
    write_row: bool = True,
) -> bool:
    """Set the orchestrator's router+key and write the chat<->session registry row.

    Returns True when the orchestrator was bound (router+key set), False when the
    feature is off / unavailable / no key was supplied.

    ``write_row=False`` (044 T20 fix round 1, Critical 1) binds the orchestrator —
    router, key, ``_public_session`` — WITHOUT touching the durable
    chat<->session row. A ROOM SERVICE run needs the binding so its replies route
    into the room and the PUBLIC profile applies, but it must NEVER become the
    session that room resolves to: ``registry.bind`` upserts on ``session_key``,
    so every service tick would re-point the room key at a one-shot session, the
    next human line would land inside the service rubric (an owner answered with
    ``[SILENT]``), the real room session would be orphaned, and the refreshed
    ``updated_at`` would keep the idle reset from ever firing. Publish still
    resolves the chat id by key, from the LIVE session's row — the same chat.
    """
    from core.surfaces.config import SurfaceConfig

    if not SurfaceConfig.singular_chat_enabled():
        return False
    if container is None or not chat_session_key:
        return False
    router = container.get_service("message_router")
    if router is None:
        return False

    orchestrator._message_router = router
    orchestrator._chat_session_key = chat_session_key
    # 044 T4: a room-bound session is PUBLIC — every injector and the tool gate
    # read this ONE flag (core/surfaces/room_policy.py::is_public_session).
    # ⚠️ 044 C2: this is a RE-affirmation, not the only stamp. Everything above
    # returns early when the bus is off, so `TaskAgent.create_session` stamps the
    # same derivation (``is_public_source``) BEFORE calling this, and the
    # recreate path restores it from the session's durable metadata. Never
    # narrow this to an assignment that can un-set a stamp made upstream: a
    # `chat_type` this call does not know about must not down-grade a session
    # that is already PUBLIC.
    from core.surfaces.room_policy import is_public_source
    if is_public_source(session_source):
        orchestrator._public_session = True
    elif not getattr(orchestrator, "_public_session", False):
        orchestrator._public_session = False

    # Write the durable chat<->session row so publish can resolve key -> surface.
    # Needs the SessionSource (surface_id/chat_id); without it we still bind the
    # mirror but publish will fail-open-drop until a row exists.
    if session_source is not None and write_row:
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


def _room_identity(container: Any, surface_id: Any, chat_id: str,
                   fallback_name: str) -> tuple:
    """``(name, instructions)`` for one room from its ``chat.*`` policy.

    Fail-open: a policy that cannot be read costs the room its NAME and its
    standing instructions, never the surface block.
    """
    try:
        from core.runtime_paths import data_dir_or_home
        from core.surfaces.chat_policy import load_for_chat
        cfg = getattr(container, "config", None)
        policy = load_for_chat(data_dir_or_home(getattr(cfg, "data_dir", None)),
                               str(surface_id or ""), chat_id)
        return (policy.name or fallback_name), policy.instructions
    except Exception as e:
        logger.debug("room identity unresolved for %s:%s (%s)", surface_id, chat_id, e)
        return fallback_name, ""


def _room_paid_note(container: Any, surface_id: Any, chat_id: str) -> str:
    """This room's paid-action paragraph for the `<surface>` block, or "".

    Fail-open: an unreadable overlay costs the model the DESCRIPTION, never the
    turn — and never grants anything, because there is nothing here to grant.
    """
    try:
        from core.surfaces.room_actions import describe_for_model
        return describe_for_model(container, surface=str(surface_id or ""),
                                  chat_id=chat_id)
    except Exception as e:
        logger.debug("room paid note unresolved for %s:%s (%s)", surface_id,
                     chat_id, e)
        return ""


def surface_profile(orchestrator: Any) -> Optional[dict]:
    """What the agent needs to know about the surface it is speaking into.

    ``SurfaceCapabilities`` has always known the message cap, the media
    capability and the markdown flavor; none of it ever reached the model, so
    the agent wrote the same reply for a 4096-byte phone chat, a 100 KB email
    and a terminal (chat-first review 2026-08-22, G6).

    Returns None when nothing is bound (goal/cron/`polyrob run`/raw API) — the
    surface-agnostic case, where the prompt stays byte-identical to today.
    Per-session stable, so injecting it does not disturb prompt caching.
    """
    key = getattr(orchestrator, "_chat_session_key", None)
    container = getattr(orchestrator, "container", None)
    if not key or container is None:
        return None
    try:
        registry = container.get_service("session_chat_registry")
        row = registry.resolve(key) if registry is not None else None
        if not row:
            # 044 T20 fix round 2 (N1): same gap as MessageRouter.publish — a room
            # with no durable row would otherwise lose its `<surface>` block, so a
            # service run there would not even know it is speaking into a room.
            from core.surfaces.session_chat_registry import row_from_session_key
            row = row_from_session_key(key)
            if not row:
                return None
            logger.warning("surface profile: no chat row for %s; reading the key "
                           "(%s:%s)", key, row["surface_id"], row["chat_id"])
        surface_id = row.get("surface_id")
        surface_registry = container.get_service("surface_registry")
        surface = surface_registry.get(surface_id) if surface_registry else None
        if surface is None:
            return None
        caps = surface.capabilities
        # 044 T15: WHICH chat, and whether it is a room. The key's 4th segment is
        # the chat_type (core/surfaces/session_chat_registry.py::build_session_key),
        # so this needs no second lookup and cannot disagree with the routing key.
        _parts = str(key).split(":")
        _chat_id = str(row.get("chat_id") or "")
        _chat_type = (_parts[3] if len(_parts) > 3 else "dm")
        # 044 T17: the room's own name and standing instructions, from the
        # per-chat policy. A DM has none, and a room with no policy file falls
        # back to the chat id — never pretty, always true.
        _name, _instructions = _chat_id, ""
        _paid = ""
        if _chat_type != "dm":
            _name, _instructions = _room_identity(container, surface_id, _chat_id, _chat_id)
            # 046 T-help: what this ROOM sells to its own members. A DESCRIPTION,
            # never a capability — the room session keeps the read-only 044
            # toolset and the denied invoice verb. Without it the model had never
            # heard of the rail, so asked "can you mute him?" it either denied a
            # shipped capability or invented a way to perform it itself.
            _paid = _room_paid_note(container, surface_id, _chat_id)
        return {
            "surface_id": str(surface_id or ""),
            "max_message_bytes": int(getattr(caps, "max_message_bytes", 0) or 0),
            "media_out": bool(getattr(caps, "media_out", False)),
            "markdown_flavor": str(getattr(caps, "markdown_flavor", "none")),
            "supports_interactive_ask": bool(
                getattr(caps, "supports_interactive_ask", False)),
            "chat_id": _chat_id,
            "chat_type": _chat_type,
            "chat_name": _name,
            "chat_instructions": _instructions,
            "chat_paid_actions": _paid,
        }
    except Exception as e:  # fail-open: unknown -> no surface block
        logger.debug("surface_profile failed: %s", e)
        return None


def bind_terminal_surface(orchestrator: Any) -> None:
    """Mark a session whose replies a FOREGROUND terminal renders live.

    The REPL and one-shot ``polyrob run`` draw every ``send_message`` from the
    session feed, but they bind no ``_message_router``/``_chat_session_key``,
    so ``maybe_deliver_autonomous_send`` read them as a session with NO
    surface and pushed each chat reply through the owner delivery rail —
    dedup, the hourly rate limit and the daily cap included. Past the cap the
    tool told the model its reply was "NOT delivered", the model re-sent an
    apology (a second bubble on the terminal), and the turn closed ``failed``
    (2026-09-17). Process-local by design: a later resume in another process
    rebuilds the orchestrator without it and keeps the durable rail.
    """
    try:
        orchestrator._terminal_attached = True
    except Exception:  # a frozen/odd orchestrator: the rail stays as it was
        logger.debug("bind_terminal_surface: could not mark orchestrator", exc_info=True)


def terminal_attached(orchestrator: Any) -> bool:
    """True when :func:`bind_terminal_surface` marked this orchestrator."""
    return bool(getattr(orchestrator, "_terminal_attached", False))


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
