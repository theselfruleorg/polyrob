"""Authorize and bound paid media work before downloads or transcription."""
import contextvars
import logging

from core.rate_limit import SlidingWindowLimiter
from core.surfaces.access import AccessTier, resolve_access_tier

logger = logging.getLogger(__name__)

_LIMIT = SlidingWindowLimiter(max_calls=10, window_seconds=60, max_keys=10000)

#: CHAT-4: True while the current task transcribes the OWNER's voice. The
#: transcriber keeps a slot for it, so a room member's long note cannot starve
#: the owner. Set by :func:`paid_media_allowed`, read by the transcriber.
OWNER_VOICE: contextvars.ContextVar = contextvars.ContextVar("owner_voice", default=False)

#: The longest voice note / the largest audio file worth a download (CHAT-4).
#: Checked against what the platform DECLARES, before any byte is fetched.
MAX_VOICE_SECONDS = 600
MAX_VOICE_BYTES = 20 * 1024 * 1024


def declared_voice_too_large(duration, file_size) -> bool:
    """True when a platform-declared duration/size is over the bound."""
    try:
        if duration is not None and float(duration) > MAX_VOICE_SECONDS:
            return True
        if file_size is not None and int(file_size) > MAX_VOICE_BYTES:
            return True
    except (TypeError, ValueError):
        return False
    return False


def ogg_duration_seconds(data) -> float | None:
    """The play length of an Ogg/Opus (or Ogg/Vorbis-at-48k) clip, or None.

    Reads the granule position of the LAST Ogg page — no decoder needed. Used
    where the platform declares no duration (WhatsApp sends only a media id),
    so the CHAT-4 bound is measured on the fetched bytes before transcription.
    None for anything that is not a readable Ogg stream (no bound is guessed).
    """
    if not data or not bytes(data[:4]) == b"OggS":
        return None
    i = bytes(data).rfind(b"OggS")
    if i < 0 or i + 14 > len(data):
        return None
    granule = int.from_bytes(bytes(data[i + 6:i + 14]), "little", signed=True)
    if granule <= 0:
        return None
    return granule / 48000.0


def voice_bytes_too_large(data) -> bool:
    """True when fetched voice bytes break the CHAT-4 bound (size or measured length)."""
    if data is None:
        return False
    if len(data) > MAX_VOICE_BYTES:
        return True
    secs = ogg_duration_seconds(data)
    return secs is not None and secs > MAX_VOICE_SECONDS


def _correspondent_model_on() -> bool:
    try:
        from core.surfaces.config import SurfaceConfig
        return bool(SurfaceConfig.correspondent_access_enabled())
    except Exception:
        return True   # fail toward the stricter tier model


def _room_allows(container, inbound, tier) -> bool:
    """A room's ``chat.mode`` (off/listen/mention) gates voice like text (CHAT-4)."""
    source = inbound.identity.source
    if (getattr(source, "chat_type", "dm") or "dm") == "dm":
        return True
    from core.runtime_paths import data_dir_or_home
    from core.surfaces import chat_policy
    cfg = getattr(container, "config", None) if container else None
    policy = chat_policy.load_for_chat(data_dir_or_home(getattr(cfg, "data_dir", None)),
                                       source.surface_id, str(source.chat_id))
    role = getattr(inbound.identity, "chat_role", None) or (
        "owner" if tier == AccessTier.OWNER else "member")
    return chat_policy.mode_allows_trigger(
        policy, mentioned=bool(inbound.mentions_bot), role=role, wake_hit=False)


def paid_media_allowed(container, inbound) -> bool:
    try:
        OWNER_VOICE.set(False)
        tier = resolve_access_tier(container, inbound.identity)
        source = inbound.identity.source
        if tier == AccessTier.DENIED:
            # With the correspondent model OFF a Telegram DM was already let in
            # by the ALLOWED_TELEGRAM_USER_IDS gate (before any work): its voice
            # follows its text, rate-limited like everyone else's.
            if not (source.surface_id == "telegram"
                    and (getattr(source, "chat_type", "dm") or "dm") == "dm"
                    and not _correspondent_model_on()):
                return False
        if tier == AccessTier.GROUP_MEMBER and not inbound.mentions_bot:
            return False
        if not _room_allows(container, inbound, tier):
            return False
        if not _LIMIT.check((source.surface_id, inbound.identity.user_id)):
            return False
        OWNER_VOICE.set(tier == AccessTier.OWNER)
        return True
    except Exception:
        logger.debug("paid media gate failed (refusing)", exc_info=True)
        return False
