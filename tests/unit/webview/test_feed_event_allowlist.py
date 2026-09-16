"""043 A16 — the feed accepts and formats the typed ``tool_result`` event.

The read-side allowlist (`GET /api/session/{id}/feed/events?event_type=…`) omitted
both `tool_execution` and the new `tool_result`, so `?event_type=tool_result` was a
400. A `ToolResultFormatter` is registered so the event never falls through to the
generic formatter.
"""
import importlib
import json
import time

import pytest
from fastapi.testclient import TestClient


def _client(monkeypatch):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("ENV", "development")
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi)


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    for key in ("POLYROB_POSTURE", "ENV"):
        monkeypatch.delenv(key, raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)


def test_tool_result_formatter_is_registered():
    from agents.task.telemetry.formatters import (
        get_formatter_registry, ToolResultFormatter, GenericEventFormatter,
    )
    reg = get_formatter_registry()
    fmt = reg.get_formatter("tool_result")
    assert isinstance(fmt, ToolResultFormatter)
    assert not isinstance(fmt, GenericEventFormatter)


def test_tool_result_formatter_shape():
    from agents.task.telemetry.formatters import ToolResultFormatter
    from types import SimpleNamespace

    event = SimpleNamespace(
        name="tool_result",
        properties={
            "tool_name": "coding",
            "action_name": "run_tests",
            "success": True,
            "render": {"kind": "text", "payload": {"text": "18 passed"}},
            "artifact_id": "a_x",
            "result_preview": "18 passed",
            "call_id": None,
            "step": 2,
        },
    )
    out = ToolResultFormatter().format(event)
    assert out["type"] == "tool_result"
    assert out["data"]["render"]["kind"] == "text"
    assert out["data"]["artifact_id"] == "a_x"
    assert out["data"]["result_preview"] == "18 passed"


def _make_feed(tmp_path, monkeypatch, sid):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    import agents.task.path as pth
    pth.reset_path_manager()
    from core.identity import ANON_USER_ID

    clean = pth.pm().clean_session_id(sid)
    feed_dir = pth.pm().get_feed_dir(clean, user_id=ANON_USER_ID)
    feed_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": time.time(),
        "type": "tool_result",
        "data": {
            "tool_name": "coding",
            "action_name": "run_tests",
            "success": True,
            "render": {"kind": "text", "payload": {"text": "18 passed"}},
            "artifact_id": None,
        },
    }
    (feed_dir / f"tool_result_{int(time.time() * 1000)}.json").write_text(json.dumps(entry))
    return feed_dir


def test_feed_events_accepts_tool_result(monkeypatch, tmp_path):
    sid = "sess-e2-a"
    _make_feed(tmp_path, monkeypatch, sid)
    client = _client(monkeypatch)

    resp = client.get(f"/api/session/{sid}/feed/events?event_type=tool_result")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    types = [e.get("type") for e in body["events"]]
    assert "tool_result" in types


def test_feed_events_accepts_tool_execution(monkeypatch, tmp_path):
    sid = "sess-e2-b"
    _make_feed(tmp_path, monkeypatch, sid)  # any existing feed dir is enough
    client = _client(monkeypatch)

    # tool_execution was also omitted from the allowlist — now accepted (not 400).
    resp = client.get(f"/api/session/{sid}/feed/events?event_type=tool_execution")
    assert resp.status_code == 200, resp.text


def test_feed_events_still_rejects_unknown_type(monkeypatch, tmp_path):
    sid = "sess-e2-c"
    _make_feed(tmp_path, monkeypatch, sid)
    client = _client(monkeypatch)

    resp = client.get(f"/api/session/{sid}/feed/events?event_type=bogus_kind")
    assert resp.status_code == 400, resp.text
