from tests.support.owner_prefs import set_owner_pref
from core.surfaces.config import SurfaceConfig


def test_echo_default_on(monkeypatch):
    set_owner_pref(monkeypatch, "voice.transcript_echo", True)
    assert SurfaceConfig.voice_transcript_echo_enabled() is True


def test_echo_off(monkeypatch):
    set_owner_pref(monkeypatch, "voice.transcript_echo", False)
    assert SurfaceConfig.voice_transcript_echo_enabled() is False
