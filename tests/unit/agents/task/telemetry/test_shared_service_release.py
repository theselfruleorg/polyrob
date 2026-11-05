"""102 N-6: one telemetry service in the process, and a real flush at session end.

``cleanup`` awaited the sync ``flush_buffers``: the flush ran, then the ``TypeError`` was
swallowed at debug. It reached only the manager's own service, so the ``get_telemetry()``
instance that the Registry writes to kept each session's buffer for the process lifetime.
"""
import json

import pytest

from agents.task.path import reset_path_manager
from agents.task.telemetry import get_telemetry
from agents.task.telemetry.manager import TelemetryManager
from agents.task.telemetry.views import ControllerRegisteredFunctionsTelemetryEvent

S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    reset_path_manager()
    svc = get_telemetry()
    monkeypatch.setattr(svc, "_buffer_size", 1000)
    monkeypatch.setattr(svc, "_buffer_timeout", 10_000)
    monkeypatch.setattr(svc, "_last_flush_times", {})
    monkeypatch.setattr(svc, "_event_buffer", {})
    monkeypatch.setattr(svc, "_feed_write_counts", {})
    monkeypatch.setattr(svc, "_should_save_to_file", lambda: True)
    # A last flush "now": the first event stays in the buffer.
    import time
    svc._last_flush_times.update({S1: time.time(), S2: time.time()})
    yield svc
    reset_path_manager()


def _capture(svc, session_id):
    svc.capture(ControllerRegisteredFunctionsTelemetryEvent(registered_functions=[]),
                session_id=session_id)


def test_every_manager_uses_the_one_service():
    assert TelemetryManager(S1)._service is TelemetryManager(S2)._service is get_telemetry()


def test_flush_buffers_writes_this_session_only(service):
    _capture(service, S1)
    _capture(service, S2)
    assert service._event_buffer[S1] and service._event_buffer[S2]
    TelemetryManager(S1).flush_buffers()
    assert service._event_buffer[S1] == []
    assert service._event_buffer[S2], "another session's buffer stays"


def test_release_session_writes_the_events_and_forgets_the_session(service):
    _capture(service, S1)
    _capture(service, S2)
    service._feed_write_counts[S1] = 3
    telemetry_dir = service._ensure_telemetry_dir(S1)

    TelemetryManager(S1).release_session()

    assert S1 not in service._event_buffer
    assert S1 not in service._last_flush_times
    assert S1 not in service._feed_write_counts
    assert S2 in service._event_buffer
    lines = (telemetry_dir / "events.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["name"] == "controller_registered_functions"


def test_cleanup_does_not_await_the_sync_flush():
    """The bug's own shape: a release is a sync call, and it does not raise on None."""
    assert TelemetryManager(S1).release_session() is None
