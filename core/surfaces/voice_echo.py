"""Shared voice-transcript echo builder. Mirrors core.surfaces.voice_guard: one place so
every surface renders an identical persistent 'here's what I heard' message before the
agent answers. Transport-free."""
from typing import List, Optional

from core.surfaces.media import Media

_ECHO_CAP = 3500  # keep under Telegram's 4096 limit; the FULL transcript still routes to the agent


def voice_transcript(media: List[Media]) -> Optional[str]:
    """Return the first voice/audio Media's stamped transcript (stripped), else None."""
    for m in media or []:
        if getattr(m, "kind", None) in ("voice", "audio"):
            t = getattr(m, "transcript", None)
            if t and t.strip():
                return t.strip()
    return None


def voice_echo_message(transcript: str) -> str:
    """Render the persistent echo shown before the agent's answer. Capped, ellipsis on overflow."""
    t = (transcript or "").strip()
    if len(t) > _ECHO_CAP:
        t = t[:_ECHO_CAP].rstrip() + "…"
    return f'🎙️ Transcript: "{t}"'
