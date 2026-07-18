"""019 P1 — RunActivity snapshot: feed events → per-session phase machine."""
import pytest

from agents.task.telemetry import run_activity


@pytest.fixture(autouse=True)
def _clean():
    run_activity._reset_for_tests()
    yield
    run_activity._reset_for_tests()


def test_unknown_session_is_none():
    assert run_activity.get_activity("nope") is None


def test_tool_span_sets_and_clears():
    run_activity.note_feed_event("s1", "tool_started",
                                 {"action_name": "navigate", "call_id": "c1"}, step=3)
    activity = run_activity.get_activity("s1")
    assert activity["phase"] == "tool"
    assert activity["detail"] == "navigate"
    assert activity["call_id"] == "c1"
    assert activity["step"] == 3
    assert activity["seconds_in_state"] >= 0.0
    run_activity.note_feed_event("s1", "tool_execution", {"call_id": "c1"})
    assert run_activity.get_activity("s1")["phase"] == "idle"


def test_completion_of_other_phase_does_not_clobber():
    """A tool completion must not wipe an approval wait that superseded it."""
    run_activity.note_feed_event("s1", "tool_started", {"action_name": "send_email"})
    run_activity.note_feed_event("s1", "awaiting_approval", {"action_name": "send_email"})
    run_activity.note_feed_event("s1", "tool_execution", {})
    assert run_activity.get_activity("s1")["phase"] == "awaiting_approval"
    run_activity.note_feed_event("s1", "approval_resolved", {"decision": "approved"})
    assert run_activity.get_activity("s1")["phase"] == "idle"


def test_thinking_compacting_retrying_delegating_phases():
    run_activity.note_feed_event("s1", "llm_started", {"model_name": "m1"})
    activity = run_activity.get_activity("s1")
    assert (activity["phase"], activity["detail"]) == ("thinking", "m1")

    run_activity.note_feed_event("s1", "compaction_started", {"mode": "llm"})
    assert run_activity.get_activity("s1")["phase"] == "compacting"
    run_activity.note_feed_event("s1", "compaction_finished", {"mode": "llm"})
    assert run_activity.get_activity("s1")["phase"] == "idle"

    run_activity.note_feed_event("s1", "retry_wait",
                                 {"reason": "rate_limit", "delay_sec": 8.0})
    activity = run_activity.get_activity("s1")
    assert activity["phase"] == "retrying"
    assert "rate_limit" in activity["detail"]

    run_activity.note_feed_event("s1", "subagent_started", {"goal_preview": "research x"})
    assert run_activity.get_activity("s1")["phase"] == "delegating"
    run_activity.note_feed_event("s1", "subagent_finished", {"ok": True})
    assert run_activity.get_activity("s1")["phase"] == "idle"


def test_session_completion_is_done():
    run_activity.note_feed_event("s1", "tool_started", {"action_name": "x"})
    run_activity.note_feed_event("s1", "session_completion", {})
    assert run_activity.get_activity("s1")["phase"] == "done"


def test_sessions_are_isolated_and_bounded():
    run_activity.note_feed_event("s1", "tool_started", {"action_name": "a"})
    run_activity.note_feed_event("s2", "llm_started", {"model_name": "m"})
    assert run_activity.get_activity("s1")["phase"] == "tool"
    assert run_activity.get_activity("s2")["phase"] == "thinking"
    # cap: oldest evicted, never unbounded
    for i in range(run_activity._MAX_SESSIONS + 10):
        run_activity.note_feed_event(f"bulk{i}", "llm_started", {})
    assert len(run_activity._activities) <= run_activity._MAX_SESSIONS


def test_never_raises_on_garbage():
    run_activity.note_feed_event("", "tool_started", {})
    run_activity.note_feed_event("s1", "", None)
    run_activity.note_feed_event("s1", "tool_started", "not-a-dict")  # type: ignore[arg-type]
    assert run_activity.get_activity("s1")["phase"] == "tool"


def test_feed_writer_wiring_updates_snapshot(tmp_path, monkeypatch):
    """The ONE choke point: ProductTelemetry._save_to_feed_directory folds every
    feed event into the snapshot (no emit site calls note_feed_event itself)."""
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

    service._save_to_feed_directory(
        ToolStartedEvent(agent_id="agent_sX", step=1, tool_name="browser",
                         action_name="navigate", parameters={}, call_id="c1",
                         session_id="sX"),
        "sX",
    )
    activity = run_activity.get_activity("sX")
    assert activity is not None and activity["phase"] == "tool"
    assert activity["detail"] == "navigate"



def test_busy_session_survives_eviction_pressure():
    """Review-fix regression: eviction is least-recently-UPDATED, so a
    long-lived session that keeps emitting is never evicted by newcomers."""
    run_activity.note_feed_event("busy", "llm_started", {"model_name": "m"})
    for i in range(run_activity._MAX_SESSIONS + 50):
        run_activity.note_feed_event(f"new{i}", "llm_started", {})
        if i % 100 == 0:  # the busy session keeps updating throughout
            run_activity.note_feed_event("busy", "tool_started",
                                         {"action_name": "work"})
    assert run_activity.get_activity("busy") is not None
    assert run_activity.get_activity("busy")["phase"] == "tool"
