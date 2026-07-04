from agents.task.surface_config import SurfaceConfig


def test_echo_default_on(monkeypatch):
    monkeypatch.delenv("VOICE_TRANSCRIPT_ECHO", raising=False)
    assert SurfaceConfig.voice_transcript_echo_enabled() is True


def test_echo_off(monkeypatch):
    monkeypatch.setenv("VOICE_TRANSCRIPT_ECHO", "false")
    assert SurfaceConfig.voice_transcript_echo_enabled() is False
