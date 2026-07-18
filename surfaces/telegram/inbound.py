"""P4: Telegram inbound pipeline — dedup -> identify -> route (transport-free).

The aiogram/FastAPI webhook handler is a thin shell over process_update: it hands the
raw Telegram update dict here, gets back a routing decision (or None when the update
is a redelivery / not a routable message), and then ACTS on the decision (STEER ->
submit_user_message, TASK_AGENT -> create_session, COMMAND -> handler, CHAT_FASTPATH
-> chat agent). Keeping this as a pure function makes the whole inbound spine testable
without a bot or a network.

Ordering is load-bearing (Fusion): dedup FIRST — before identify (which writes via
UserDirectory.get_or_create_by_tg_id) and before route (which may create a session) —
so a redelivered update_id never double-processes.
"""
import logging
from typing import Any, Optional

from core.surfaces.act import InboundResult  # noqa: F401 — canonical home since R-4 (re-export)
from core.surfaces.dispatcher import route_inbound, RouteDecision
from core.surfaces.envelopes import InboundMessage, Identity, SessionSource
from core.surfaces.media import Media

logger = logging.getLogger(__name__)

# Prefix stamped onto a transcribed voice note so the AGENT knows the message arrived
# as voice and was auto-transcribed. Without it the agent sees indistinguishable plain
# text and, asked "can you understand voice?", denies having speech-to-text (the
# 2026-07-03 self-awareness bug). The clean transcript is still stamped separately on
# the voice Media for voice-echo, so this marker never leaks into the echo.
VOICE_TRANSCRIPT_PREFIX = "[voice message, auto-transcribed] "


def _chat_type(tg_type: Optional[str]) -> str:
    """Map Telegram chat.type -> our SessionSource.chat_type."""
    return "dm" if tg_type == "private" else "group"


def _detect_bot_mention(msg: dict, text: str, bot_username: Optional[str]) -> Optional[bool]:
    """W3 groups (2026-07-14): does this GROUP message address OUR bot?

    True  = an @mention entity resolving to @<bot_username> (case-insensitive), a
            ``text_mention`` entity pointing at the bot user, or a reply to one of
            the bot's own messages.
    False = bot_username is known and nothing addressed the bot.
    None  = bot_username unknown (the dispatcher gate treats None as NOT mentioned,
            preserving the fail-closed default) or not a group chat.

    Without this the telegram surface never set ``mentions_bot`` and the
    GROUP_CHAT_ENABLED mention gate silently denied EVERY group message —
    including the owner's @mentions and replies.
    """
    if not bot_username:
        return None
    uname = bot_username.lower()
    for ent in msg.get("entities") or []:
        etype = ent.get("type")
        if etype == "mention":
            try:
                offset, length = int(ent.get("offset", 0)), int(ent.get("length", 0))
                if text[offset:offset + length].lower() == f"@{uname}":
                    return True
            except (TypeError, ValueError):
                continue
        elif etype == "text_mention":
            if str(((ent.get("user") or {}).get("username") or "")).lower() == uname:
                return True
    reply_from = (msg.get("reply_to_message") or {}).get("from") or {}
    if reply_from.get("is_bot") and str(reply_from.get("username") or "").lower() == uname:
        return True
    return False


def build_inbound_message(update: dict, user_directory: Any,
                          bot_username: Optional[str] = None) -> Optional[InboundMessage]:
    """Build a normalized InboundMessage from a raw Telegram update, identifying the
    user to an INTERNAL user_id. Returns None for updates with no routable message.

    ``bot_username`` (no ``@``) enables group mention/reply detection; when omitted,
    ``mentions_bot`` stays None (gate-safe unknown).
    """
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    from_user = msg.get("from") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    text = msg.get("text") or ""
    tg_id = str(from_user.get("id")) if from_user.get("id") is not None else str(chat_id)

    # Owner alias: an authenticated Telegram owner operates as the instance OWNER
    # principal (e.g. "rob") so their chat shares autonomy's tenant (goals/memory/SELF)
    # instead of a surface-hashed u_ id. Telegram-only + owner-only + fail-open to the
    # legacy hashed id (owner_surface_alias returns None for a non-owner / unbound owner).
    from core.instance import owner_surface_alias
    user_id = owner_surface_alias(tg_id, "telegram") or user_directory.resolve_internal(
        tg_id, "telegram"
    )
    source = SessionSource(
        surface_id="telegram",
        chat_id=str(chat_id),
        chat_type=_chat_type(chat.get("type")),
    )
    update_id = update.get("update_id")

    # Populate media with a voice Media when the update carries voice/audio.
    # Bytes are fetched lazily by the harness (transport-specific); we only record
    # kind + mime here so the core voice_needs_guard seam can inspect media instead
    # of reaching back into the raw Telegram update dict.
    from surfaces.telegram.voice import extract_voice_file_id
    media: list = []
    if extract_voice_file_id(update):
        media = [Media(kind="voice", mime="audio/ogg")]

    # W3 groups: mention/reply detection so the dispatcher's group gate can pass
    # addressed messages instead of silently denying everything (2026-07-14 fix).
    mentions_bot = None
    reply_to = None
    reply_msg = msg.get("reply_to_message") or {}
    if reply_msg.get("message_id") is not None:
        reply_to = str(reply_msg["message_id"])
    if _chat_type(chat.get("type")) == "group":
        mentions_bot = _detect_bot_mention(msg, text, bot_username)

    return InboundMessage(
        text=text,
        identity=Identity(
            user_id=user_id, source=source,
            raw_user_id=tg_id, display_name=from_user.get("username"),
        ),
        idempotency_key=str(update_id) if update_id is not None else None,
        raw=update,
        media=media,
        reply_to=reply_to,
        mentions_bot=mentions_bot,
    )


async def process_update(
    container: Any,
    update: dict,
    *,
    dedup: Any,
    user_directory: Any,
    is_chitchat=None,
    transcribe_voice=None,
    now: Optional[float] = None,
    bot_username: Optional[str] = None,
) -> Optional[InboundResult]:
    """Dedup -> [voice transcription] -> identify -> route. Returns None if the update
    is a redelivery or has no routable message; otherwise an InboundResult the webhook
    handler acts on.

    ``transcribe_voice`` (optional, injected by the harness so this stays transport-free)
    is an async ``(update) -> Optional[str]``; when it yields text for a voice/audio
    message, that text is injected as the message text so the rest of the pipeline
    treats the voice note exactly like a typed message (#9).
    """
    update_id = update.get("update_id")
    # 1) DEDUP FIRST (before any side-effecting identify/route, and before the
    #    network-bound transcription, so a redelivery never re-downloads/re-transcribes).
    if update_id is not None and dedup is not None:
        try:
            if dedup.seen(update_id, now=now):
                logger.debug("telegram inbound: dropping redelivered update %s", update_id)
                return None
        except Exception as e:  # fail-open: a dedup error must not drop a real update
            logger.debug("telegram dedup check failed (processing anyway): %s", e)

    # 1b) VOICE -> TEXT (after dedup). Inject the transcript as the message text.
    voice_text = None
    if transcribe_voice is not None:
        try:
            voice_text = await transcribe_voice(update)
            if voice_text:
                msg = update.get("message") or update.get("edited_message") or {}
                # Mark it as voice for the agent; keep the raw transcript for the media
                # stamp below (voice-echo) so the echo shows the clean transcription.
                msg["text"] = f"{VOICE_TRANSCRIPT_PREFIX}{voice_text}"
        except Exception as e:  # fail-open: a transcription error must not drop the update
            logger.debug("telegram voice transcription skipped: %s", e)

    # 2) IDENTIFY.
    inbound = build_inbound_message(update, user_directory, bot_username=bot_username)
    if inbound is None:
        return None
    # Stamp the transcript onto the voice Media so the surface can echo it (voice_echo).
    if voice_text and inbound.media:
        inbound.media[0].transcript = voice_text

    # 3) ROUTE.
    decision = await route_inbound(container, inbound, is_chitchat=is_chitchat)
    return InboundResult(inbound=inbound, decision=decision)
