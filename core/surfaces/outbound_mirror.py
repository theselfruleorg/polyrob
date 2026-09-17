"""Outbound-collapse mirror helpers (P1a).

Small factories that mirror an agent's discrete user-facing message into the
unified MessageRouter seam as a committed (partial=False) OutboundMessage. Additive
and gated on SINGULAR_CHAT_ENABLED; a no-op (and fail-open) when the flag is OFF or
no router / session_key has been bound, so the legacy add_to_feed path stays
byte-identical until later phases wire a router.
"""
import logging
from typing import Any, Awaitable, Callable, Optional

from core.surfaces.room_keys import is_group_session_key

logger = logging.getLogger(__name__)


def build_discrete_publish(
    router: Any, session_key: Optional[str], *,
    session_id: Optional[str] = None,
    workspace_dir: Optional[str] = None,
    media_ok: bool = False,
    scanner: Any = None,
    reply_to: Optional[str] = None,
    replied_ids: Optional[list] = None,
) -> Callable[..., Awaitable[None]]:
    """Mirror one agent-authored message onto the bound surface.

    C4: when the caller supplies the session context, any workspace path in the
    body is resolved to something the reader can actually open — attached, else a
    console URL, else an honest "server-only" note. Omitting that context keeps
    the legacy call shape byte-identical, so every existing caller is unaffected.

    ``replied_ids`` (044 T20) is the run's own list of ledger ids it answered.
    Appended ONLY when this publish targets a ROOM and carries an anchor — a DM
    has no room ledger, so a DM reply must never land in it. This is the one
    place that knows both facts, which is why the recording lives here rather
    than at the action.
    """
    async def _publish(text: str):
        """Returns the path :class:`Resolution` when one was computed, else None —
        so the caller can report to the AGENT what happened to its files."""
        from core.surfaces.config import SurfaceConfig

        if not SurfaceConfig.singular_chat_enabled():
            return None
        if router is None or not session_key:
            return None
        media: list = []
        resolution = None
        # C4: a filesystem path is not an address. Fail-open — a resolver fault
        # must cost a link, never the message.
        if workspace_dir:
            try:
                from core.surfaces.path_links import resolve_paths
                resolution = resolve_paths(
                    text, session_id=session_id, workspace_dir=workspace_dir,
                    media_ok=media_ok, scanner=scanner)
                text, media = resolution.text, resolution.attachments
            except Exception as e:
                logger.debug("path resolution skipped (fail-open): %s", e)
                resolution = None
        try:
            from core.surfaces.envelopes import OutboundMessage, MessageKind

            delivered = await router.publish(OutboundMessage(
                session_key=session_key,
                text=text,
                kind=MessageKind.AGENT_TEXT,
                partial=False,
                media=media,
                reply_to=reply_to,
            ))
            # Fix round 1 (Minor 7): record the answer only when `publish` says
            # it DELIVERED. A `[SILENT]`, a capped room, a dead target or a send
            # failure must not mark the line answered — the room never heard it.
            # `is True` on purpose: a legacy router double that returns None is
            # not making that claim, and under-recording only costs a re-read.
            if delivered is True and replied_ids is not None and reply_to:
                if is_group_session_key(session_key) and str(reply_to) not in replied_ids:
                    replied_ids.append(str(reply_to))
        except Exception as e:  # fail-open: outbound mirror is non-critical
            logger.debug("discrete publish mirror failed: %s", e)
        return resolution

    return _publish
