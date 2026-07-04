from core.surfaces.media import Media
from core.surfaces.voice_guard import voice_needs_guard, voice_unavailable_message


def test_guard_fires_on_voice_with_empty_text():
    assert voice_needs_guard([Media(kind="voice", data=b"x")], "") is True
    assert voice_needs_guard([Media(kind="voice", data=b"x")], "   ") is True


def test_guard_silent_when_text_present():
    assert voice_needs_guard([Media(kind="voice", data=b"x")], "hello") is False


def test_guard_silent_when_no_voice():
    assert voice_needs_guard([Media(kind="image")], "") is False


def test_unavailable_message_differs_by_enabled():
    assert "off" in voice_unavailable_message(False).lower()
    assert "couldn't" in voice_unavailable_message(True).lower() or \
           "could not" in voice_unavailable_message(True).lower()
