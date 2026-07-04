"""Surface-agnostic voice→text seam. The Transcriber is built ONCE and registered on the
container (was a lazy per-Telegram-harness instance — invisible to Email/WhatsApp). Every
surface normalizes inbound audio to Media (core.surfaces.media) and calls
transcribe_inbound_media(); the untranscribed-voice outcome is handled by one shared guard
(core.surfaces.voice_guard) so no surface can silently route an empty voice turn."""
import logging
from typing import List, Optional

from core.surfaces.media import Media

logger = logging.getLogger(__name__)


def voice_present(media: List[Media]) -> bool:
    return any(getattr(m, "kind", None) in ("voice", "audio") for m in (media or []))


def get_transcriber(container):
    """Build-once, container-registered Transcriber. Falls back to NullTranscriber when
    faster-whisper isn't importable (build_transcriber already degrades). Registered so
    every surface shares one model load instead of each harness building its own."""
    existing = container.get_service("transcriber") if container else None
    if existing is not None:
        return existing
    from agents.task.surface_config import SurfaceConfig
    from modules.transcription import build_transcriber
    t = build_transcriber(SurfaceConfig.voice_transcription_model())
    try:
        container.register_service("transcriber", t)
    except Exception:
        pass
    return t


async def _audio_bytes(media: Media) -> Optional[bytes]:
    if media.data:
        return media.data
    if media.url:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(media.url) as r:
                    return await r.read()
        except Exception as e:
            logger.debug("transcription: media url fetch failed: %s", e)
            return None
    return None


async def transcribe_inbound_media(container, media: List[Media]) -> Optional[str]:
    """Transcribe the first voice/audio Media to text, or None. Fail-open."""
    from agents.task.surface_config import SurfaceConfig
    if not SurfaceConfig.voice_transcription_enabled():
        return None
    target = next((m for m in (media or []) if getattr(m, "kind", None) in ("voice", "audio")), None)
    if target is None:
        return None
    audio = await _audio_bytes(target)
    if not audio:
        return None
    try:
        transcriber = get_transcriber(container)
        text = await transcriber.transcribe(audio, mime=target.mime or "audio/ogg")
    except Exception as e:  # belt-and-suspenders; transcriber is itself fail-open
        logger.debug("transcribe_inbound_media failed: %s", e)
        return None
    return (text or "").strip() or None


def log_transcription_readiness(container) -> None:
    """One-line startup signal so a 'voice silently dropped' deploy is visible. WARN when
    voice is enabled but the real engine isn't importable (the #1 root cause)."""
    from agents.task.surface_config import SurfaceConfig
    if not SurfaceConfig.voice_transcription_enabled():
        logger.info("voice transcription: DISABLED (VOICE_TRANSCRIPTION_ENABLED=false)")
        return
    try:
        import faster_whisper  # noqa: F401
        logger.info("voice transcription: ENABLED (faster-whisper present, model=%s)",
                    SurfaceConfig.voice_transcription_model())
    except Exception:
        msg = ("voice transcription ENABLED but faster-whisper is NOT installed — voice "
               "messages will be refused with a notice. Install the 'voice' extra or set "
               "VOICE_TRANSCRIPTION_ENABLED=false.")
        if SurfaceConfig.voice_transcription_required():
            logger.error("STARTUP: %s", msg)
        else:
            logger.warning("STARTUP: %s", msg)
