"""faster-whisper transcriber. The model loads lazily on first use and inference runs
in a worker thread (faster-whisper is sync + CPU-bound), so a long transcription never
blocks the event loop. Fail-open: any error returns "" (no transcript)."""
import asyncio
import logging
import os
import tempfile
from typing import Optional

from .base import Transcriber

logger = logging.getLogger(__name__)


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
        if not audio:
            return ""
        try:
            return await asyncio.to_thread(self._transcribe_sync, audio, language)
        except Exception as e:  # fail-open: a transcription failure must not drop the msg
            logger.warning("faster-whisper transcription failed: %s", e)
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
