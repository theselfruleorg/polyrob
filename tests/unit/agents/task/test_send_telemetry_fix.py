"""send_telemetry passed user_id= to ProductTelemetry.capture(), which has no such
param -> TypeError (swallowed, always returned False). Telemetry audit 2026-07-04:
drop the unsupported kwarg so the function actually works if ever called.
"""
import types


def test_send_telemetry_calls_capture_without_user_id(monkeypatch):
    import agents.task.utils as utils

    captured = {}

    class _FakeTel:
        def capture(self, event, session_id=None):  # NOTE: no user_id param
            captured["event"] = event
            captured["session_id"] = session_id

    monkeypatch.setattr("agents.task.telemetry.get_telemetry", lambda: _FakeTel())

    class _FakePM:
        def clean_session_id(self, s):
            return s

    monkeypatch.setattr("agents.task.path.pm", lambda: _FakePM())
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "true")

    ok = utils.send_telemetry(types.SimpleNamespace(name="x"), session_id="s1", user_id="u1")
    assert ok is True, "send_telemetry should not crash on the user_id kwarg anymore"
    assert captured["session_id"] == "s1"
