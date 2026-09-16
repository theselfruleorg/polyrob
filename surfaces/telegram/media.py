"""Telegram attachment extraction (2026-09-13 chat-media rail).

Turns the attachment fields of a Telegram update into the transport-agnostic
``core.surfaces.media.Media`` envelope. Only extraction lives here — the download
is ``voice.download_file_bytes`` and the workspace write is
``core.surfaces.inbound_attachments``, so this module stays pure and testable
without a bot.

Ordering note: ``voice``/``audio`` are listed LAST. They are the transcription
path's territory (``extract_voice_file_id``), and a voice note that transcribes
must keep producing exactly one ``voice`` Media so ``voice_guard`` and
``voice_echo`` behave as before.
"""
import logging
from typing import Any, List, Optional, Tuple

from core.surfaces.media import Media

logger = logging.getLogger(__name__)

#: (update field, Media kind, default extension) in the order we probe them.
#: ``photo`` is special-cased (it is a LIST of sizes), so it is not in this table.
_FIELDS = (
    ("document", "document", None),
    ("video", "video", ".mp4"),
    ("animation", "video", ".mp4"),
    ("video_note", "video", ".mp4"),
    ("sticker", "sticker", ".webp"),
    ("audio", "audio", ".ogg"),
    ("voice", "voice", ".ogg"),
)


def _message(update: dict) -> dict:
    return update.get("message") or update.get("edited_message") or {}


def _largest_photo(sizes: list) -> Optional[dict]:
    """Telegram sends an ascending ladder of thumbnails; the owner meant the last
    one. Sorting by file_size (falling back to width) survives a provider that
    does not guarantee the order."""
    usable = [s for s in sizes if isinstance(s, dict) and s.get("file_id")]
    if not usable:
        return None
    return max(usable, key=lambda s: (s.get("file_size") or 0, s.get("width") or 0))


def _sticker_ext(item: dict) -> str:
    """A sticker is three different payloads behind one field. Naming an animated
    (.tgs, gzipped Lottie) or video (.webm) sticker `.webp` would send it into the
    vision path as a broken image."""
    if item.get("is_animated"):
        return ".tgs"
    if item.get("is_video"):
        return ".webm"
    return ".webp"


def extract_media(update: dict) -> List[Media]:
    """Every attachment on the update as ``Media`` (``ref`` = Telegram file_id).

    Bytes are NOT fetched here — the harness downloads lazily, so an update we end
    up dropping never costs a network round trip.
    """
    msg = _message(update)
    caption = msg.get("caption") or None
    out: List[Media] = []

    photo = msg.get("photo")
    if isinstance(photo, list):
        best = _largest_photo(photo)
        if best:
            out.append(Media(kind="image", mime="image/jpeg", ref=str(best["file_id"]),
                             caption=caption, filename=f"photo_{best['file_id'][:12]}.jpg"))

    for field, kind, default_ext in _FIELDS:
        item = msg.get(field)
        if not isinstance(item, dict) or not item.get("file_id"):
            continue
        filename = item.get("file_name")
        if not filename:
            ext = _sticker_ext(item) if field == "sticker" else (default_ext or "")
            filename = f"{field}_{str(item['file_id'])[:12]}{ext}"
        out.append(Media(kind=kind, mime=item.get("mime_type"),
                         ref=str(item["file_id"]), caption=caption, filename=filename))
    return out


# --- the session-facing half of the rail -----------------------------------
# Lives here, not in ``harness.py``: harness.py is on the status-silence ratchet
# (``tests/test_status_silence_ratchet.py``) because it renders /status, and the
# media rail's fail-open handlers have nothing to do with that contract. Keeping
# them out of the ratcheted file is the ratchet's own remedy — extract, don't grow.

async def absorb_for_session(task_agent: Any, result: Any, session_id: str,
                             fetch_media, *, base_text: str = "") -> Tuple[str, Optional[dict]]:
    """Store this message's attachments in ``session_id``'s workspace and describe them.

    Returns ``(text, metadata)`` ready for ``submit_user_message``. Fail-open: any
    fault leaves the turn exactly as it arrived — an attachment must never cost the
    owner their message.
    """
    media = getattr(getattr(result, "inbound", None), "media", None)
    if not media or fetch_media is None:
        return base_text, None

    # A session with no on-disk metadata is about to be replaced by a fresh one (the
    # STEER "gone" branch). Absorbing here would create an orphan workspace and store
    # the files twice; let the fresh session absorb them instead. An unreadable
    # session store is treated as "absorb" — a stored file is recoverable, a lost
    # one is not.
    try:
        if not task_agent.session_manager.get_session_info(session_id):
            return base_text, None
    except Exception as e:
        logger.debug("telegram media: session lookup failed for %s, absorbing anyway: %s",
                     session_id, e)

    try:
        from agents.task.path import pm
        from core.surfaces.inbound_attachments import absorb_inbound_media

        workspace_dir = str(pm().get_workspace_dir(
            session_id, result.inbound.identity.user_id))
        text, images = await absorb_inbound_media(
            media, workspace_dir, fetch_bytes=fetch_media, base_text=base_text)
    except Exception as e:
        logger.warning("telegram media absorb failed for %s: %s", session_id, e,
                       exc_info=True)
        return base_text, None

    logger.info("telegram media absorbed: session=%s files=%d images=%d",
                session_id, len(media), len(images or []))
    return text, ({"image_attachments": images} if images else None)


async def queue_attachments_for_new_session(task_agent: Any, result: Any,
                                            session_id: str, fetch_media,
                                            *, extra_metadata: Optional[dict] = None) -> bool:
    """Queue a fresh session's attachments BEFORE its run starts.

    The caption is NOT repeated here — it is already the session's ``request``, so
    this turn carries only the attachment description and its vision blocks.
    Returns whether anything was queued.

    ``extra_metadata`` merges into the queued message's metadata (044 T10 fix
    round 1: a fresh room session's reply-anchor rides here too — this is the
    FIRST message ``_drain_user_messages`` will ever see for the session, and
    without the anchor on it, that drain would clear the anchor the harness
    set at creation time right back to None).
    """
    inbound = getattr(result, "inbound", None)
    if not getattr(inbound, "media", None) or fetch_media is None:
        return False
    text, metadata = await absorb_for_session(task_agent, result, session_id,
                                              fetch_media, base_text="")
    if not text.strip():
        return False
    if extra_metadata:
        metadata = {**(metadata or {}), **extra_metadata}
    try:
        await task_agent.ensure_session_and_deliver(
            inbound.identity.user_id, session_id, text.strip(),
            kind="comment", metadata=metadata,
        )
    except Exception as e:
        logger.warning("telegram attachment queue failed for %s: %s", session_id, e)
        return False
    return True
