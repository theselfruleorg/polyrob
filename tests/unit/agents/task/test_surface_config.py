"""SurfaceConfig session-boundary defaults (a6 + #7): reset-mode defaults to `idle`
everywhere now that the recreate-race (#2) and mute-on-resume (#0) fixes that make a
reset safe have landed. The idle WINDOW still differs (720 local / 1440 server)."""
from agents.task.surface_config import SurfaceConfig


def test_session_reset_mode_default_idle_under_local(monkeypatch):
    monkeypatch.delenv("SESSION_RESET_MODE", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert SurfaceConfig.session_reset_mode() == "idle"


def test_session_reset_mode_default_idle_on_server(monkeypatch):
    # #7: server default flipped none -> idle (gated on #0+#2, now landed).
    monkeypatch.delenv("SESSION_RESET_MODE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    assert SurfaceConfig.session_reset_mode() == "idle"


def test_session_reset_mode_explicit_none_still_honored(monkeypatch):
    # Operators can still pin the legacy inert behavior explicitly.
    monkeypatch.setenv("SESSION_RESET_MODE", "none")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    assert SurfaceConfig.session_reset_mode() == "none"


def test_session_reset_mode_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("SESSION_RESET_MODE", "both")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert SurfaceConfig.session_reset_mode() == "both"


def test_session_idle_minutes_local_vs_server(monkeypatch):
    monkeypatch.delenv("SESSION_IDLE_MINUTES", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert SurfaceConfig.session_idle_minutes() == 720
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    assert SurfaceConfig.session_idle_minutes() == 1440
