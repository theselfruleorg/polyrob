import pytest
from core.surfaces.media import Media
from core.surfaces.transcription import (
    voice_present, transcribe_inbound_media, get_transcriber,
)


class _FakeContainer:
    def __init__(self): self._svc = {}
    def get_service(self, k): return self._svc.get(k)
    def register_service(self, k, v): self._svc[k] = v


class _FakeTranscriber:
    def __init__(self, text): self._text = text
    async def transcribe(self, audio, *, mime=None, language=None): return self._text


def test_voice_present_detects_voice_media():
    assert voice_present([Media(kind="image"), Media(kind="voice", data=b"x")]) is True
    assert voice_present([Media(kind="image")]) is False


def test_get_transcriber_is_build_once():
    c = _FakeContainer()
    t1 = get_transcriber(c)
    t2 = get_transcriber(c)
    assert t1 is t2  # same instance reused via the container


@pytest.mark.asyncio
async def test_transcribe_inbound_media_returns_text(monkeypatch):
    c = _FakeContainer()
    c.register_service("transcriber", _FakeTranscriber("hello world"))
    out = await transcribe_inbound_media(c, [Media(kind="voice", data=b"\x00\x01")])
    assert out == "hello world"


@pytest.mark.asyncio
async def test_transcribe_inbound_media_empty_returns_none():
    c = _FakeContainer()
    c.register_service("transcriber", _FakeTranscriber("   "))
    out = await transcribe_inbound_media(c, [Media(kind="voice", data=b"\x00")])
    assert out is None
