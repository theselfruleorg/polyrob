"""One shared voice guard so NO surface can silently route an untranscribed voice note.
A voice message that produced no usable transcript -> the surface tells the user (loudly)
and does NOT dispatch an empty turn to the agent (the live bug: empty voice turns reaching
the agent, which then 'understood nothing')."""
from typing import List

from core.surfaces.media import Media


def voice_needs_guard(media: List[Media], inbound_text) -> bool:
    """True iff the inbound carries voice/audio but yielded no usable text."""
    has_voice = any(getattr(m, "kind", None) in ("voice", "audio") for m in (media or []))
    if not has_voice:
        return False
    return not (inbound_text or "").strip()


def voice_unavailable_message(enabled: bool) -> str:
    if not enabled:
        return ("🎤 I can't process voice messages right now — voice transcription is "
                "turned off. Please send your message as text.")
    return ("🎤 I couldn't transcribe that voice message (it may be empty, too noisy, or "
            "transcription isn't available on this server). Please try again or send text.")
