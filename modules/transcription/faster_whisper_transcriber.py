"""faster-whisper transcriber. The model loads lazily on first use and inference runs
in a worker thread (faster-whisper is sync + CPU-bound), so a long transcription never
blocks the event loop. Fail-open: any error returns "" (no transcript)."""
import asyncio
import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .base import Transcriber

logger = logging.getLogger(__name__)
# One CPU inference at a time across containers/surfaces. Capacity belongs to
# the actual worker future, not an async caller that can time out or cancel.
_SLOTS = threading.BoundedSemaphore(1)
#: CHAT-4: one more slot only the OWNER's voice may take, so a room member's
#: long note cannot starve the owner's (``core.surfaces.media_access.OWNER_VOICE``).
_OWNER_SLOT = threading.BoundedSemaphore(1)
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voice-transcription")


def _owner_voice() -> bool:
    try:
        from core.surfaces.media_access import OWNER_VOICE
        return OWNER_VOICE.get() is True
    except Exception:
        return False
INFERENCE_TIMEOUT_SECONDS = 60
MAX_AUDIO_BYTES = 20 * 1024 * 1024


class FasterWhisperTranscriber(Transcriber):
    def __init__(self, model_size: str = "base", device: str = "cpu",
                 compute_type: str = "int8") -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model = None  # lazily constructed on first transcribe

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy: heavy import + optional extra
            self._model = WhisperModel(
                self._model_size, device=self._device, compute_type=self._compute_type
            )
        return self._model

    async def transcribe(self, audio: bytes, *, mime: Optional[str] = None,
                         language: Optional[str] = None) -> str:
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            return ""
        slots = _SLOTS
        if not slots.acquire(blocking=False):
            slots = _OWNER_SLOT
            if not (_owner_voice() and slots.acquire(blocking=False)):
                logger.warning("voice transcription unavailable: inference already running")
                return ""
        try:
            future = _EXECUTOR.submit(self._transcribe_guarded, audio, language)
        except BaseException:
            slots.release()
            raise
        future.add_done_callback(lambda _: slots.release())
        try:
            return await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)), INFERENCE_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("voice transcription timed out; inference capacity remains reserved")
            return ""

    def _transcribe_guarded(self, audio, language):
        try:
            return self._transcribe_sync(audio, language)
        except Exception as exc:
            logger.warning("faster-whisper transcription failed (%s)", type(exc).__name__)
            return ""

    def _transcribe_sync(self, audio: bytes, language: Optional[str]) -> str:
        model = self._ensure_model()
        # faster-whisper reads from a path; Telegram voice is OGG/Opus, which ffmpeg
        # (pulled in by faster-whisper) decodes by content, so the suffix is cosmetic.
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio)
            path = f.name
        try:
            segments, _info = model.transcribe(path, language=language)
            return "".join(seg.text for seg in segments).strip()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
