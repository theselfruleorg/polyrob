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
    async def get_file(self, file_id, request_timeout=None):
        self.got.append(file_id)
        return type("F", (), {"file_path": f"voice/{file_id}.ogg"})()
    async def download_file(self, file_path, destination=None, timeout=30):
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


def test_an_oversized_voice_note_is_never_downloaded():
    """CHAT-4: the declared duration/size is checked before any download."""
    from surfaces.telegram.voice import extract_voice_file_id
    long = {"message": {"voice": {"file_id": "v", "duration": 3600}}}
    big = {"message": {"audio": {"file_id": "a", "file_size": 50 * 1024 * 1024}}}
    ok = {"message": {"voice": {"file_id": "v", "duration": 12, "file_size": 40000}}}
    assert extract_voice_file_id(long) is None
    assert extract_voice_file_id(big) is None
    assert extract_voice_file_id(ok) == "v"


@pytest.mark.asyncio
async def test_a_download_gets_the_long_timeout():
    seen = {}

    class _Bot(_FakeBot):
        async def download_file(self, file_path, destination=None, timeout=30):
            seen["timeout"] = timeout
            return await super().download_file(file_path)

    assert await download_voice_bytes(_Bot(b"VIDEO"), "v1") == b"VIDEO"
    assert seen["timeout"] == 120


@pytest.mark.asyncio
async def test_a_failed_download_logs_the_reason_at_warning(caplog):
    class _Bot(_FakeBot):
        async def get_file(self, file_id, request_timeout=None):
            raise RuntimeError("file is too big")

    with caplog.at_level("WARNING", logger="surfaces.telegram.voice"):
        assert await download_voice_bytes(_Bot(), "v1") is None
    assert "file is too big" in caplog.text


class _StreamBot(_FakeBot):
    def __init__(self, size=None):
        super().__init__()
        self.size = size
        self.downloads = []

    async def get_file(self, file_id, request_timeout=None):
        self.request_timeout = request_timeout
        return type("F", (), {"file_path": "/srv/video.mp4", "file_size": self.size})()

    async def download_file(self, file_path, destination=None, timeout=30):
        self.downloads.append(destination)
        with open(destination, "wb") as stream:
            stream.write(b"VIDEO")


@pytest.mark.asyncio
async def test_a_video_streams_to_its_spool_file(tmp_path):
    from pathlib import Path

    from core.surfaces.media import Media
    from surfaces.telegram.media import fetch_media_file

    spool = tmp_path / "spool"
    bot = _StreamBot()
    got = await fetch_media_file(bot, Media(kind="video", ref="v1", spool=str(spool)))
    assert got == Path(spool)
    assert spool.read_bytes() == b"VIDEO"
    assert bot.request_timeout == 60


@pytest.mark.asyncio
async def test_a_file_over_the_cap_at_get_file_is_not_downloaded(tmp_path):
    from surfaces.telegram.voice import download_file_to_path

    bot = _StreamBot(size=11)
    assert await download_file_to_path(bot, "v1", str(tmp_path / "s"), 10) is False
    assert bot.downloads == []


@pytest.mark.asyncio
async def test_a_local_server_gets_the_long_get_file_timeout(tmp_path, monkeypatch):
    from surfaces.telegram.voice import LOCAL_GET_FILE_TIMEOUT_SECONDS, download_file_to_path

    monkeypatch.setenv("TELEGRAM_BOT_API_URL", "http://127.0.0.1:8081")
    bot = _StreamBot()
    assert await download_file_to_path(bot, "v1", str(tmp_path / "s"), 10)
    assert bot.request_timeout == LOCAL_GET_FILE_TIMEOUT_SECONDS
