"""019 P2 — live per-turn progress tracker (feed events → progress bubble)."""
import asyncio

import pytest

from agents.task.telemetry import live_progress
from agents.task.telemetry.live_progress import TurnProgressTracker


class _FakeReporter:
    def __init__(self):
        self.stages = []
        self._finished = False

    async def stage(self, text):
        self.stages.append(text)

    async def finish(self):
        self._finished = True


@pytest.fixture(autouse=True)
def _clean_registry():
    live_progress._reset_for_tests()
    yield
    live_progress._reset_for_tests()


def _tracker(reporter=None, **kw):
    kw.setdefault("session_id", "s1")
    kw.setdefault("min_edit_interval", 0.01)
    return TurnProgressTracker(reporter or _FakeReporter(), **kw)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_compose_working_line_with_step_tool_and_count():
    now = [100.0]
    tracker = _tracker(clock=lambda: now[0])
    tracker._step = 3
    tracker._current = "→ navigate"
    tracker._tools = 2
    now[0] = 145.0
    text = tracker.compose_text()
    assert text.startswith("⚙️ step 3 · → navigate · 2 tools · 45s")


def test_compose_bare_start_keeps_legacy_wording():
    tracker = _tracker()
    assert tracker.compose_text() == "⚙️ Working…"


def test_wait_state_overrides_composition():
    tracker = _tracker()
    tracker.on_feed_event({"type": "awaiting_approval",
                           "data": {"action_name": "send_email"}})
    assert "Waiting for your approval" in tracker.compose_text()
    tracker.on_feed_event({"type": "approval_resolved",
                           "data": {"decision": "approved"}})
    assert "Waiting" not in tracker.compose_text()


def test_retry_and_compaction_wait_states():
    tracker = _tracker()
    tracker.on_feed_event({"type": "retry_wait",
                           "data": {"reason": "rate_limit", "delay_sec": 8.0}})
    assert tracker.compose_text() == "↻ rate_limit — retrying in 8s"
    tracker.on_feed_event({"type": "compaction_started", "data": {"mode": "llm"}})
    assert tracker.compose_text() == "📦 Compacting context…"
    tracker.on_feed_event({"type": "compaction_finished", "data": {}})
    assert "Compacting" not in tracker.compose_text()


# ---------------------------------------------------------------------------
# Edit scheduling (throttle + immediate wait states)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_drive_throttled_edits():
    reporter = _FakeReporter()
    tracker = _tracker(reporter)
    tracker.on_feed_event({"type": "tool_started",
                           "data": {"action_name": "navigate", "tool_name": "browser"}})
    await asyncio.sleep(0.05)
    assert any("→ navigate" in s for s in reporter.stages)


@pytest.mark.asyncio
async def test_wait_state_edits_immediately():
    reporter = _FakeReporter()
    tracker = _tracker(reporter, min_edit_interval=999.0)
    tracker.on_feed_event({"type": "tool_started", "data": {"action_name": "x"}})
    tracker.on_feed_event({"type": "awaiting_approval",
                           "data": {"action_name": "send_email"}})
    await asyncio.sleep(0.05)
    assert any("Waiting for your approval" in s for s in reporter.stages)


@pytest.mark.asyncio
async def test_trailing_edit_lands_after_interval():
    reporter = _FakeReporter()
    tracker = _tracker(reporter, min_edit_interval=0.1)
    tracker.on_feed_event({"type": "tool_started", "data": {"action_name": "one"}})
    tracker.on_feed_event({"type": "tool_started", "data": {"action_name": "two"}})
    await asyncio.sleep(0.3)
    assert any("→ two" in s for s in reporter.stages)


@pytest.mark.asyncio
async def test_approval_reminder_fires_once():
    reporter = _FakeReporter()
    tracker = _tracker(reporter, approval_reminder_sec=0.05)
    tracker.on_feed_event({"type": "awaiting_approval",
                           "data": {"action_name": "send_email"}})
    await asyncio.sleep(0.2)
    reminders = [s for s in reporter.stages if "Still waiting" in s]
    assert len(reminders) == 1


@pytest.mark.asyncio
async def test_close_stops_edits():
    reporter = _FakeReporter()
    tracker = _tracker(reporter, min_edit_interval=0.05)
    tracker.on_feed_event({"type": "tool_started", "data": {"action_name": "one"}})
    tracker.close()
    baseline = len(reporter.stages)
    tracker.on_feed_event({"type": "tool_started", "data": {"action_name": "two"}})
    await asyncio.sleep(0.15)
    assert len(reporter.stages) == baseline


# ---------------------------------------------------------------------------
# Lazy binding + dispatch registry
# ---------------------------------------------------------------------------


def test_try_match_binds_via_resolver():
    tracker = TurnProgressTracker(
        _FakeReporter(), session_key="telegram:123", session_id=None,
        key_resolver=lambda sid: "telegram:123" if sid == "sess9" else None,
    )
    assert not tracker.try_match("other")
    assert tracker.try_match("sess9")
    assert tracker.session_id == "sess9"
    assert tracker.try_match("sess9")  # bound → exact compare


@pytest.mark.asyncio
async def test_dispatch_routes_and_lazily_binds():
    reporter = _FakeReporter()
    tracker = TurnProgressTracker(
        reporter, session_key="telegram:123",
        key_resolver=lambda sid: "telegram:123" if sid == "sess9" else None,
        min_edit_interval=0.01,
    )
    live_progress.attach_tracker(tracker)
    live_progress._dispatch("unrelated", {"type": "tool_started",
                                          "data": {"action_name": "zzz"}})
    live_progress._dispatch("sess9", {"type": "tool_started",
                                      "data": {"action_name": "navigate"}})
    await asyncio.sleep(0.05)
    assert any("→ navigate" in s for s in reporter.stages)
    assert not any("zzz" in s for s in reporter.stages)
    tracker.close()
    assert live_progress._by_session == {}


def test_feed_subscriber_registration_idempotent():
    from agents.task.telemetry.service import ProductTelemetry

    calls = []

    def _cb(sid, ev):
        calls.append((sid, ev))

    ProductTelemetry.add_feed_subscriber(_cb)
    ProductTelemetry.add_feed_subscriber(_cb)
    assert ProductTelemetry._feed_subscribers.count(_cb) == 1
    ProductTelemetry.remove_feed_subscriber(_cb)
    ProductTelemetry.remove_feed_subscriber(_cb)  # no-op
    assert _cb not in ProductTelemetry._feed_subscribers


def test_feed_writer_fires_subscribers(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from agents.task.telemetry.service import ProductTelemetry
    from agents.task.telemetry.views import ToolStartedEvent

    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    service = ProductTelemetry()
    fake_pm = MagicMock()
    fake_pm.clean_session_id.side_effect = lambda x: x
    fake_pm.get_subdir.return_value = feed_dir
    monkeypatch.setattr("agents.task.telemetry.service.pm", lambda: fake_pm)

    received = []
    ProductTelemetry.add_feed_subscriber(lambda sid, ev: received.append((sid, ev)))
    try:
        service._save_to_feed_directory(
            ToolStartedEvent(agent_id="agent_sZ", step=1, tool_name="browser",
                             action_name="navigate", parameters={}, session_id="sZ"),
            "sZ",
        )
    finally:
        ProductTelemetry._feed_subscribers.clear()
    assert received and received[0][0] == "sZ"
    assert received[0][1]["type"] == "tool_started"
