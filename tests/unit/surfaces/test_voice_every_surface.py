"""A voice note becomes a turn the same way on every surface that receives audio."""
from types import SimpleNamespace

import pytest

from core.surfaces.media import Media
from surfaces._shared import BaseHarness
from surfaces.telegram.media import absorb_for_session


class _Container:
    def __init__(self, text):
        self.transcriber = SimpleNamespace(transcribe=self._transcribe)
        self.text = text
        self.heard = []

    async def _transcribe(self, audio, *, mime=None, language=None):
        self.heard.append(audio)
        return self.text

    def get_service(self, name):
        return self.transcriber if name == "transcriber" else None


class _Harness(BaseHarness):
    surface_id = "discord"

    def __init__(self, container, audio=b"opus"):
        super().__init__(container, SimpleNamespace(), SimpleNamespace())
        self.audio = audio
        self.fetches = 0

    async def _fetch_media(self, media):
        self.fetches += 1
        return self.audio


def _inbound(text="", media=None):
    return SimpleNamespace(text=text, media=media if media is not None else [
        Media(kind="audio", mime="audio/ogg", url="https://cdn.example/a.ogg")])


@pytest.fixture
def gate(monkeypatch):
    allowed = {"v": True}
    monkeypatch.setattr("core.surfaces.media_access.paid_media_allowed",
                        lambda _c, _i: allowed["v"])
    monkeypatch.delenv("VOICE_TRANSCRIPTION_ENABLED", raising=False)
    return allowed


async def _run(harness, inbound):
    said = []

    async def deliver(text):
        said.append(text)
    return await harness._voice_to_text(inbound, deliver), said


@pytest.mark.asyncio
async def test_a_transcript_becomes_the_turn_and_is_echoed(gate):
    container = _Container("what is the time")
    harness, inbound = _Harness(container), _inbound()
    run, said = await _run(harness, inbound)
    assert run is True
    assert inbound.text == "[voice message, auto-transcribed] what is the time"
    assert inbound.media[0].transcript == "what is the time"
    assert container.heard == [b"opus"]
    assert said == ['🎙️ Transcript: "what is the time"']
    # The rail stores the same note without a second download.
    assert await harness._media_bytes(inbound.media[0]) == b"opus"
    assert harness.fetches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text,audio", [("", b"opus"), ("", None)])
async def test_no_transcript_tells_the_sender_and_runs_no_turn(gate, text, audio):
    harness = _Harness(_Container(text), audio=audio)
    run, said = await _run(harness, _inbound())
    assert run is False
    assert len(said) == 1 and "couldn't transcribe" in said[0]


@pytest.mark.asyncio
async def test_a_refused_sender_costs_no_download_and_routes_as_before(gate):
    gate["v"] = False
    harness = _Harness(_Container("words"))
    inbound = _inbound()
    run, said = await _run(harness, inbound)
    assert (run, said, harness.fetches, inbound.text) == (True, [], 0, "")


@pytest.mark.asyncio
async def test_typed_words_stay_and_the_audio_stays_an_attachment(gate):
    harness = _Harness(_Container("words"))
    inbound = _inbound(text="see this clip")
    run, said = await _run(harness, inbound)
    assert (run, said, harness.fetches, inbound.text) == (True, [], 0, "see this clip")


@pytest.mark.asyncio
async def test_a_seat_with_no_downloader_does_not_call_a_transcribed_note_lost():
    """WhatsApp: the words of the note sat next to "could NOT store them"."""
    result = SimpleNamespace(inbound=SimpleNamespace(media=[Media(kind="voice", transcript="hi")]))
    text, metadata = await absorb_for_session(
        SimpleNamespace(), result, "s", None, base_text="[voice message, auto-transcribed] hi")
    assert text == "[voice message, auto-transcribed] hi"
    assert metadata == {"typed_text": text}


@pytest.mark.asyncio
async def test_a_seat_with_no_downloader_names_only_the_real_files():
    result = SimpleNamespace(inbound=SimpleNamespace(media=[
        Media(kind="voice"), Media(kind="document", filename="a.pdf")]))
    text, _ = await absorb_for_session(SimpleNamespace(), result, "s", None, base_text="hi")
    assert "a.pdf" in text and "1 attachment(s)" in text


def test_whatsapp_audio_document_stays_a_document(tmp_path):
    """`hydrate_media` reads `filename` as the Graph media id; a document's is a name."""
    from core.surfaces.idempotency import IdempotencyStore
    from surfaces.whatsapp.inbound import WhatsAppInbound

    class _Directory:
        def resolve_internal(self, raw, surface):
            return "u_" + raw

    surface = WhatsAppInbound(IdempotencyStore(str(tmp_path / "i.db")), user_directory=_Directory())
    message = {"from": "100", "id": "m1", "type": "document", "document": {
        "id": "G1", "mime_type": "audio/mpeg", "filename": "talk.mp3"}}
    payload = {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}
    assert surface.parse(payload)[0].media[0].kind == "document"
