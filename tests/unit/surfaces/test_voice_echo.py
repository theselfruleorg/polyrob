from core.surfaces.media import Media
from core.surfaces.voice_echo import voice_transcript, voice_echo_message, _ECHO_CAP


def test_voice_transcript_returns_stamped():
    assert voice_transcript([Media(kind="voice", transcript="turn on the lights")]) == "turn on the lights"


def test_voice_transcript_none_paths():
    assert voice_transcript([Media(kind="voice")]) is None
    assert voice_transcript([Media(kind="image")]) is None
    assert voice_transcript([]) is None
    assert voice_transcript(None) is None


def test_voice_echo_message_wording():
    assert voice_echo_message("hello world") == '🎙️ Transcript: "hello world"'


def test_voice_echo_message_strips():
    assert voice_echo_message("  hi  ") == '🎙️ Transcript: "hi"'


def test_voice_echo_message_caps_long_text():
    out = voice_echo_message("a" * (_ECHO_CAP + 50))
    assert out.endswith('…"')
    assert len(out) < _ECHO_CAP + 40
