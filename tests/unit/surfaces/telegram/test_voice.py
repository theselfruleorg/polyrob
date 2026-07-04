"""#9 voice transcription. The engine is surface-agnostic (modules/transcription); only
the file extraction + download is Telegram's job (surfaces/telegram/voice.py); and
process_update gets the transcriber injected so it stays transport-free.
"""
import pytest

from surfaces.telegram.voice import (
    extract_voice_file_id, download_voice_bytes, transcribe_telegram_voice,
)
from surfaces.telegram.inbound import process_update


# --- shared engine factory degrades, never crashes --------------------------

def test_build_transcriber_degrades_to_null_without_extra(monkeypatch):
    import modules.transcription as mt

    def _boom(*a, **k):
        raise ImportError("no faster_whisper")
    # Force the faster-whisper path to fail -> NullTranscriber fallback.
    monkeypatch.setattr(
        "modules.transcription.faster_whisper_transcriber.FasterWhisperTranscriber",
        _boom, raising=True,
    )
    t = mt.build_transcriber("base")
    assert t.__class__.__name__ == "NullTranscriber"


@pytest.mark.asyncio
async def test_null_transcriber_returns_empty():
    from modules.transcription import NullTranscriber
    assert await NullTranscriber().transcribe(b"abc") == ""


# --- Telegram extraction + download -----------------------------------------

def test_extract_voice_file_id_from_voice_and_audio():
    assert extract_voice_file_id({"message": {"voice": {"file_id": "v1"}}}) == "v1"
    assert extract_voice_file_id({"message": {"audio": {"file_id": "a1"}}}) == "a1"
    assert extract_voice_file_id({"message": {"text": "hi"}}) is None


class _FakeBot:
    def __init__(self, blob=b"OGGDATA"):
        self.blob = blob
        self.got = []
    async def get_file(self, file_id):
        self.got.append(file_id)
        return type("F", (), {"file_path": f"voice/{file_id}.ogg"})()
    async def download_file(self, file_path):
        import io
        return io.BytesIO(self.blob)


@pytest.mark.asyncio
async def test_download_voice_bytes_reads_binaryio():
    bot = _FakeBot(b"AUDIO")
    data = await download_voice_bytes(bot, "v1")
    assert data == b"AUDIO"
    assert bot.got == ["v1"]


class _FakeTranscriber:
    def __init__(self, text="hello world"):
        self.text = text
        self.calls = []
    async def transcribe(self, audio, *, mime=None, language=None):
        self.calls.append((audio, mime))
        return self.text


@pytest.mark.asyncio
async def test_transcribe_telegram_voice_end_to_end():
    bot = _FakeBot(b"AUDIO")
    tr = _FakeTranscriber("transcribed text")
    out = await transcribe_telegram_voice(
        bot, {"message": {"voice": {"file_id": "v1"}}}, tr)
    assert out == "transcribed text"
    assert tr.calls and tr.calls[0][0] == b"AUDIO"


@pytest.mark.asyncio
async def test_transcribe_telegram_voice_none_for_text_message():
    bot = _FakeBot()
    tr = _FakeTranscriber()
    out = await transcribe_telegram_voice(bot, {"message": {"text": "hi"}}, tr)
    assert out is None


# --- process_update injection (transport-free) ------------------------------

class _FakeDedup:
    def seen(self, update_id, now=None):
        return False


class _FakeUD:
    def resolve_internal(self, tg_id, source):
        return "u_" + str(tg_id)


@pytest.mark.asyncio
async def test_process_update_injects_transcript_as_text(monkeypatch):
    """A voice update with no text gets the transcript injected so routing treats it as
    a normal typed message — now MARKED as voice (VOICE_TRANSCRIPT_PREFIX) so the agent
    knows it can process voice."""
    seen = {}

    async def _transcribe(update):
        return "spoken question"

    update = {
        "update_id": 7,
        "message": {"chat": {"id": 555, "type": "private"},
                    "from": {"id": 42}, "voice": {"file_id": "v1"}},
    }
    # Stub routing so we only assert the injected inbound text.
    import surfaces.telegram.inbound as inbound_mod

    async def _fake_route(container, inbound, is_chitchat=None):
        seen["text"] = inbound.text
        from core.surfaces.dispatcher import RouteDecision, RouteKind
        return RouteDecision(RouteKind.TASK_AGENT, "k")
    monkeypatch.setattr(inbound_mod, "route_inbound", _fake_route)

    result = await process_update(
        container=None, update=update,
        dedup=_FakeDedup(), user_directory=_FakeUD(),
        transcribe_voice=_transcribe,
    )
    from surfaces.telegram.inbound import VOICE_TRANSCRIPT_PREFIX
    assert result is not None
    assert seen["text"] == f"{VOICE_TRANSCRIPT_PREFIX}spoken question"
    assert result.inbound.text == f"{VOICE_TRANSCRIPT_PREFIX}spoken question"


@pytest.mark.asyncio
async def test_process_update_no_transcriber_is_unchanged(monkeypatch):
    """Without a transcriber injected, a text update routes exactly as before."""
    update = {
        "update_id": 8,
        "message": {"chat": {"id": 555, "type": "private"},
                    "from": {"id": 42}, "text": "typed"},
    }
    import surfaces.telegram.inbound as inbound_mod

    async def _fake_route(container, inbound, is_chitchat=None):
        from core.surfaces.dispatcher import RouteDecision, RouteKind
        return RouteDecision(RouteKind.TASK_AGENT, "k")
    monkeypatch.setattr(inbound_mod, "route_inbound", _fake_route)

    result = await process_update(
        container=None, update=update,
        dedup=_FakeDedup(), user_directory=_FakeUD(),
    )
    assert result.inbound.text == "typed"
