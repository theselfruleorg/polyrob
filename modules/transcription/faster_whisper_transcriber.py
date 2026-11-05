"""faster-whisper transcriber. The model loads lazily on first use and inference runs
in a worker thread (faster-whisper is sync + CPU-bound), so a long transcription never
blocks the event loop. Fail-open: any error returns "" (no transcript)."""
import asyncio
import gc
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
#: Measured on Linux (102 M-P): 370 MB after one note; del gives 26 MB back, and
#: malloc_trim 258 MB more. A reload takes about 1 s, so a child process is not needed.
IDLE_UNLOAD_S = 900.0


def _return_freed_memory() -> None:
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):  # no glibc: the memory stays with the allocator
        logger.debug("malloc_trim is not available")


class FasterWhisperTranscriber(Transcriber):
    def __init__(self, model_size: str = "base", device: str = "cpu",
                 compute_type: str = "int8") -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model = None  # lazily constructed on first transcribe
        self._model_lock = threading.Lock()
        self._running = 0
        self._idle_timer: Optional[threading.Timer] = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy: heavy import + optional extra
            self._model = WhisperModel(
                self._model_size, device=self._device, compute_type=self._compute_type
            )
        return self._model

    def _begin_use(self):
        """Load under the lock (two slots can race) and hold the model against the unload."""
        with self._model_lock:
            model = self._ensure_model()
            self._running += 1
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            return model

    def _end_use(self) -> None:
        with self._model_lock:
            self._running -= 1
            if self._running == 0:
                self._idle_timer = threading.Timer(IDLE_UNLOAD_S, self._unload_if_idle)
                self._idle_timer.daemon = True
                self._idle_timer.start()

    def _unload_if_idle(self) -> None:
        with self._model_lock:
            if self._running or self._model is None:
                return
            self._model = None
        gc.collect()
        _return_freed_memory()
        logger.info("voice model unloaded after %.0f s idle", IDLE_UNLOAD_S)

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
        model = self._begin_use()
        # faster-whisper reads from a path; Telegram voice is OGG/Opus, which ffmpeg
        # (pulled in by faster-whisper) decodes by content, so the suffix is cosmetic.
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio)
            path = f.name
        try:
            segments, _info = model.transcribe(path, language=language)
            return "".join(seg.text for seg in segments).strip()
        finally:
            self._end_use()
            try:
                os.unlink(path)
            except OSError:
                pass
