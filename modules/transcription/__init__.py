"""Surface-agnostic speech-to-text (#9).

The engine lives here, NOT in any surface, so every chat surface (Telegram today,
WhatsApp/etc. tomorrow) downloads its own audio and hands raw bytes to one shared
``Transcriber``. Keep transport-specific extraction in the surface; keep model/runtime
concerns here.

``build_transcriber()`` is the factory: a real faster-whisper transcriber when the
extra is installed, else a ``NullTranscriber`` (returns "") so a misconfigured deploy
degrades to "no transcript" instead of crashing the inbound spine.
"""
from .base import Transcriber, NullTranscriber

__all__ = ["Transcriber", "NullTranscriber", "build_transcriber"]


def build_transcriber(model_size: str = "base") -> Transcriber:
    """Return a ready Transcriber. Falls back to NullTranscriber if faster-whisper isn't
    importable (the model itself loads lazily on first transcribe)."""
    try:
        from .faster_whisper_transcriber import FasterWhisperTranscriber
        return FasterWhisperTranscriber(model_size=model_size)
    except Exception:  # import-time failure (extra missing) -> degrade, don't crash
        return NullTranscriber()
