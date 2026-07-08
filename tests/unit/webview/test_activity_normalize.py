"""Task 1 — activity event normalization core (webview/activity.py).

One normalized shape for every activity source:
    {id, ts, source, user_id, session_id, kind, summary, payload}
"""
import json

from webview.activity import (
    ActivityHub,
    normalize_db_event,
    normalize_feed_event,
    summarize,
)


def test_normalize_feed_event_telemetry_shape():
    raw = {
        "type": "tool_execution",
        "_seq": 42,
        "_ts_ms": 1751800000000,
        "_id": "evt-abc",
        "data": {"tool_name": "web_fetch", "success": True},
    }
    ev = normalize_feed_event("rob", "sess-1", raw)
    assert ev is not None
    assert ev["source"] == "feed"
    assert ev["user_id"] == "rob"
    assert ev["session_id"] == "sess-1"
    assert ev["kind"] == "tool_execution"
    assert ev["ts"] == 1751800000000 / 1000.0
    assert isinstance(ev["summary"], str) and ev["summary"]
    assert ev["payload"]["data"]["tool_name"] == "web_fetch"
    assert ev["id"] == "evt-abc"


def test_normalize_feed_event_session_manager_shape():
    # session.py writes {timestamp, type, data} without _seq/_ts_ms/_id
    raw = {"timestamp": 1751800001.5, "type": "status", "data": {"status": "running"}}
    ev = normalize_feed_event("rob", "sess-2", raw)
    assert ev["ts"] == 1751800001.5
    assert ev["kind"] == "status"
    assert ev["id"]  # synthesized, non-empty


def test_normalize_feed_event_suppresses_streaming_output():
    raw = {"type": "streaming_output", "_ts_ms": 1, "data": {"chunk": "hi"}}
    assert normalize_feed_event("rob", "s", raw) is None


def test_normalize_feed_event_truncates_huge_payload():
    raw = {"type": "step", "_ts_ms": 1, "data": {"blob": "x" * 50000}}
    ev = normalize_feed_event("rob", "s", raw)
    assert ev is not None
    dumped = json.dumps(ev["payload"])
    assert len(dumped) < 20000
    assert ev["payload"].get("_truncated") is True


def test_normalize_db_event_telemetry_row():
    row = {
        "id": 7,
        "ts": 1751800002.0,
        "kind": "cron_run",
        "user_id": "rob",
        "session_id": "",
        "source": "cron",
        "attrs": json.dumps({"job_id": "j1", "outcome": "ok"}),
    }
    ev = normalize_db_event("telemetry", row)
    assert ev["source"] == "telemetry"
    assert ev["kind"] == "cron_run"
    assert ev["id"] == "telemetry:7"
    assert "j1" in ev["summary"] or "cron" in ev["summary"]
    assert ev["payload"]["job_id"] == "j1"


def test_normalize_db_event_goal_row():
    row = {
        "id": 3,
        "goal_id": "g-9",
        "kind": "claimed",
        "payload": json.dumps({"title": "grow x"}),
        "created_at": 1751800003.0,
    }
    ev = normalize_db_event("goal", row)
    assert ev["kind"] == "goal_claimed"
    assert ev["ts"] == 1751800003.0
    assert "g-9" in ev["summary"]


def test_summarize_known_kinds():
    assert "web_fetch" in summarize("tool_execution", {"tool_name": "web_fetch", "success": True})
    s = summarize("llm_request", {"model_name": "grok-4.3", "token_count": 1234})
    assert "grok-4.3" in s and "1234" in s
    assert "step" in summarize("step", {"iteration": 4}).lower()
    assert summarize("totally_unknown_kind", {}) == "totally_unknown_kind"


def test_hub_ring_buffer_caps_and_orders():
    hub = ActivityHub()
    for i in range(1500):
        hub.record({"ts": float(i), "kind": "k", "source": "feed",
                    "user_id": "u", "session_id": "s", "summary": str(i), "payload": {}})
    assert len(hub.buffer) == 1000
    recent = hub.recent(10)
    assert len(recent) == 10
    assert recent[-1]["summary"] == "1499"
    assert recent[0]["summary"] == "1490"
    # ids were assigned and are unique
    ids = [e["id"] for e in recent]
    assert len(set(ids)) == 10
