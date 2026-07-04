"""Transcriber ABC + a no-op fallback. Surface-agnostic: bytes in, text out."""
from abc import ABC, abstractmethod
from typing import Optional


class Transcriber(ABC):
    @abstractmethod
    async def transcribe(
        self, audio: bytes, *, mime: Optional[str] = None, language: Optional[str] = None
    ) -> str:
        """Transcribe raw audio bytes to text. Returns "" when nothing is recognized.
        Implementations MUST be fail-open (never raise into the caller's inbound path)."""
        ...


class NullTranscriber(Transcriber):
    """Used when transcription is disabled or the faster-whisper extra is unavailable."""

    async def transcribe(self, audio: bytes, *, mime=None, language=None) -> str:
        return ""
