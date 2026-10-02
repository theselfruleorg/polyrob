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


def test_a_url_only_voice_media_is_never_downloaded(monkeypatch):
    """2026-09-23 Low: the dormant url path did a bare aiohttp GET (no SSRF
    policy, no cap). It is removed; bytes are the only input."""
    import asyncio

    import aiohttp

    from core.surfaces.media import Media
    from core.surfaces.transcription import _audio_bytes

    def _boom(*a, **k):
        raise AssertionError("transcription must not open an HTTP session")

    monkeypatch.setattr(aiohttp, "ClientSession", _boom)
    assert asyncio.run(_audio_bytes(Media(kind="voice", url="http://169.254.169.254/x"))) is None
    assert asyncio.run(_audio_bytes(Media(kind="voice", data=b"ogg"))) == b"ogg"
