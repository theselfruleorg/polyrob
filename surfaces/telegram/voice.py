"""Telegram-specific voice handling (#9): pull the audio file out of an update and
download its bytes, then hand them to the surface-agnostic Transcriber. The transcriber
itself lives in modules/transcription — only the extraction + download is Telegram's job.
"""
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def extract_voice_file_id(update: dict) -> Optional[str]:
    """Return the file_id of a voice/audio attachment on the update, or None."""
    msg = update.get("message") or update.get("edited_message") or {}
    for field in ("voice", "audio"):
        media = msg.get(field)
        if isinstance(media, dict) and media.get("file_id"):
            return str(media["file_id"])
    return None


async def download_voice_bytes(bot: Any, file_id: str) -> Optional[bytes]:
    """Resolve + download a Telegram file's bytes via the bot. Fail-open -> None."""
    try:
        f = await bot.get_file(file_id)
        file_path = getattr(f, "file_path", None)
        if file_path is None and isinstance(f, dict):
            file_path = f.get("file_path")
        data = await bot.download_file(file_path)
        if data is None:
            return None
        if hasattr(data, "read"):       # aiogram returns a BinaryIO/BytesIO
            return data.read()
        return bytes(data)
    except Exception as e:
        logger.debug("telegram voice download failed for %s: %s", file_id, e)
        return None


async def transcribe_telegram_voice(bot: Any, update: dict, transcriber: Any) -> Optional[str]:
    """If the update carries voice/audio, download + transcribe it; else None. Fail-open."""
    file_id = extract_voice_file_id(update)
    if not file_id:
        return None
    audio = await download_voice_bytes(bot, file_id)
    if not audio:
        return None
    try:
        text = await transcriber.transcribe(audio, mime="audio/ogg")
    except Exception as e:  # transcriber is fail-open, but belt-and-suspenders
        logger.debug("telegram voice transcription failed: %s", e)
        return None
    return (text or "").strip() or None
