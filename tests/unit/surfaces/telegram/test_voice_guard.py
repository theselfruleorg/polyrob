"""Voice guard: when a voice/audio note can't be transcribed, the surface must reply
with a clear 'voice unavailable' message instead of routing an empty agent turn (which
reads as a confused generic reply)."""
from core.surfaces.voice_guard import voice_needs_guard, voice_unavailable_message
from core.surfaces.media import Media


def _voice_media():
    return [Media(kind="voice", data=b"x")]


def test_guard_fires_on_voice_with_no_transcript():
    # voice note, empty inbound text (transcription off / unavailable) -> guard
    assert voice_needs_guard(_voice_media(), "") is True
    assert voice_needs_guard(_voice_media(), None) is True
    assert voice_needs_guard(_voice_media(), "   ") is True


def test_guard_skipped_when_voice_was_transcribed():
    # transcript was injected as text -> normal flow, no guard
    assert voice_needs_guard(_voice_media(), "turn on the lights") is False


def test_guard_skipped_for_plain_text_message():
    assert voice_needs_guard([], "hello") is False
    assert voice_needs_guard([], "") is False  # empty text, but not voice


def test_guard_message_distinguishes_off_vs_unavailable():
    off = voice_unavailable_message(enabled=False)
    unavail = voice_unavailable_message(enabled=True)
    assert "turned off" in off.lower()
    assert "couldn't transcribe" in unavail.lower() or "isn't available" in unavail.lower()
    assert off != unavail
