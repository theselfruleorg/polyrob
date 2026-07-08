"""T4-03: goal_run events → durable event log.

Goal runs previously wrote only the episodes table, never the event_log, so
`polyrob telemetry` (the tool built to answer "what ran?") showed cron and self-wake
but not the goals that did most of the autonomous work. _goal_ev mirrors _cron_ev.
"""
from types import SimpleNamespace


def test_goal_ev_records_to_event_log(tmp_path, monkeypatch):
    import agents.task.telemetry.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    test_log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: test_log)

    from agents.task.goals import dispatcher
    goal = SimpleNamespace(id="g1", user_id="rob", title="Post the announcement")

    dispatcher._goal_ev(goal, "started")
    dispatcher._goal_ev(goal, "done", session_id="s1", spend_usd=0.02, steps=7)
    dispatcher._goal_ev(goal, "blocked", reason="needs twitter write")

    rows = test_log.query(kind="goal_run")
    assert len(rows) == 3
    outcomes = {r["attrs"]["outcome"] for r in rows}
    assert outcomes == {"started", "done", "blocked"}
    done = [r for r in rows if r["attrs"]["outcome"] == "done"][0]
    assert done["attrs"]["steps"] == 7
    assert done["user_id"] == "rob"
    assert done["attrs"]["goal_id"] == "g1"
    assert done["source"] == "goal"


def test_goal_ev_fail_open():
    # No event log configured / broken import must not raise.
    from agents.task.goals import dispatcher
    goal = SimpleNamespace(id="g", user_id="u", title="t")
    dispatcher._goal_ev(goal, "started")  # no-op if anything fails
