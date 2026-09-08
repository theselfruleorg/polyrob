"""031 T13: the pause state leads every status seat; a violation is CRITICAL."""
import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "telemetry_events.db"))
    return tmp_path


def _snap(home):
    from core.status_snapshot import build_status_snapshot
    return build_status_snapshot("rob", data_dir=str(home), include_money=False)


def test_pause_renders_first_and_as_health(home):
    from core import autonomy_control as ac
    from core.status_render import pause_headline, render_status_lines, render_agent_health_note
    ac.pause(str(home), scopes=("all",), via="test", set_by="owner")
    snap = _snap(home)
    assert snap.sections["loops"].data["pause"]["paused"] is True
    assert snap.sections["loops"].data["halted"] is True
    assert any(h.key == "paused" for h in snap.health)
    head = pause_headline(snap)
    assert head.startswith("⏸ PAUSED (everything)") and "owner via test" in head
    assert render_status_lines(snap)[0] == head
    note = render_agent_health_note(snap)
    assert "⏸ PAUSED (everything)" in note and "autonomy_control(resume)" in note
    assert "/resume" not in note  # the agent's note names its own verb, never a slash command


def test_violation_after_pause_is_critical(home):
    from core import autonomy_control as ac
    from core.event_log import TelemetryEventLog
    ac.pause(str(home), via="test")
    log = TelemetryEventLog(str(home / "telemetry_events.db"))
    log.record("goal_run", user_id="rob", session_id="s1", source="goal",
               attrs={"outcome": "started"})
    log.record("self_wake", user_id="rob", session_id="s1", source="self_wake",
               attrs={"outcome": "paused"})  # honoured the pause: not a violation
    snap = _snap(home)
    v = [h for h in snap.health if h.key == "pause_violation"]
    assert v and v[0].severity == "crit" and "goal_run ×1" in v[0].text
    assert "self_wake" not in v[0].text


def test_running_headline_names_live_actors(home):
    from agents.task.goals.board import GoalBoard
    from core.status_render import pause_headline
    GoalBoard(str(home / "goals.db"))  # a board exists; nothing is running
    snap = _snap(home)
    head = pause_headline(snap)
    assert head.startswith("▶ RUNNING") and "/pause to stop" in head
    assert snap.sections["loops"].data["live_actors"]["running_goals"] == 0


def test_missing_stores_never_hide_the_pause_line(home):
    """A fresh install has no goals.db / cron.db: the loops section still renders
    the pause state (with the store reasons), never 'unavailable' as a whole."""
    from core import autonomy_control as ac
    from core.status_render import pause_headline
    ac.pause(str(home), via="test")
    snap = _snap(home)
    loops = snap.sections["loops"]
    assert loops.available and loops.data["pause"]["paused"] is True
    assert "cron_reason" in loops.data and loops.data["live_actors"]["running_goals"] is None
    assert pause_headline(snap).startswith("⏸ PAUSED (everything)")
