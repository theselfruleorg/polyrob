"""D3: live-info wiring — sub-agent count + in-flight tool surfaced to the user.

The sub-agent signal is NEEDS-WIRING (core's sub-agent registration is skipped;
the orchestrator's sub-agent lifecycle hooks fire but nobody subscribed). This
adds: SessionState accumulators, a testable hook factory the REPL registers on
the orchestrator, and conditional status-bar segments (omitted when zero, so the
existing toolbar is byte-identical).
"""

from __future__ import annotations

from cli.ui import statusbar
from cli.ui.events import normalize
from cli.ui.live_hooks import make_subagent_hooks
from cli.ui.state import SessionState


# ---------------------------------------------------------------------------
# SessionState accumulators
# ---------------------------------------------------------------------------


def test_subagents_active_starts_zero():
    assert SessionState().subagents_active == 0


def test_note_subagent_start_end():
    s = SessionState()
    s.note_subagent_start()
    s.note_subagent_start()
    assert s.subagents_active == 2
    s.note_subagent_end()
    assert s.subagents_active == 1
    s.note_subagent_end()
    s.note_subagent_end()  # never goes negative
    assert s.subagents_active == 0


def test_last_tool_set_from_tool_exec():
    s = SessionState()
    assert s.last_tool == ""
    ev = normalize({
        "type": "tool_execution",
        "data": {"tool_name": "anysite", "action_name": "anysite_api", "success": True},
    })
    s.update(ev)
    assert s.last_tool == "anysite_api"


# ---------------------------------------------------------------------------
# Hook factory (what the REPL registers on the orchestrator)
# ---------------------------------------------------------------------------


def test_make_subagent_hooks_mutate_state():
    s = SessionState()
    start, end = make_subagent_hooks(s)
    # Hooks are called by the orchestrator with kwargs (goal, agent_id, ...).
    start(goal="research", agent_id="sub_1", parent_session_id="p")
    start(goal="research2", agent_id="sub_2", parent_session_id="p")
    assert s.subagents_active == 2
    end(goal="research", agent_id="sub_1", parent_session_id="p", ok=True)
    assert s.subagents_active == 1


def test_make_subagent_hooks_are_fail_open():
    s = SessionState()
    start, end = make_subagent_hooks(s)
    # Missing/extra kwargs must never raise into the agent loop.
    start()                    # +1
    end(unexpected="x")        # -1
    assert s.subagents_active == 0


# ---------------------------------------------------------------------------
# Status-bar segments (conditional → byte-identical when zero)
# ---------------------------------------------------------------------------


def test_statusbar_omits_live_segments_when_zero():
    s = SessionState()
    s.model = "m"
    text = statusbar.status_text(s)
    assert "sub-agent" not in text
    assert "→" not in text


def test_statusbar_shows_subagents_when_active():
    s = SessionState()
    s.model = "m"
    s.note_subagent_start()
    s.note_subagent_start()
    assert "2 sub-agents" in statusbar.status_text(s)


def test_statusbar_shows_in_flight_tool():
    s = SessionState()
    s.model = "m"
    s.last_tool = "anysite_api"
    # The in-flight tool shows only while a turn is active (not at the idle prompt).
    s.lifecycle.begin_turn()
    assert "anysite_api" in statusbar.status_text(s)


def test_statusbar_hides_in_flight_tool_when_idle():
    s = SessionState()
    s.model = "m"
    s.last_tool = "anysite_api"
    assert "anysite_api" not in statusbar.status_text(s)
