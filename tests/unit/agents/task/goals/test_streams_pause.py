"""031 T7: the stream seeder reads the pause record; a paused objective is honoured."""


def test_stream_is_due_reads_the_record(tmp_path, monkeypatch):
    for k in ("STREAM_SEEDING_PAUSE", "DATA_ROOT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core import autonomy_control as ac
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.streams import stream_is_due
    b = GoalBoard(str(tmp_path / "goals.db"))
    stream = {"id": "s", "cadence_hours": 1, "goals": [{"title": "t"}], "objective": {"title": "o"}}
    assert stream_is_due(b, "rob", stream)[0] is True
    ac.pause(str(tmp_path), scopes=("streams",), via="test")
    due, why = stream_is_due(b, "rob", stream)
    assert due is False and "paused" in why
    ac.resume(str(tmp_path))
    ac.pause(str(tmp_path), scopes=("cron",), via="test")
    assert stream_is_due(b, "rob", stream)[0] is True


def test_ensure_objective_does_not_duplicate_a_paused_objective(tmp_path):
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.streams import ensure_objective
    b = GoalBoard(str(tmp_path / "goals.db"))
    stream = {"id": "s", "objective": {"title": "Mission", "body": "b"}, "goals": []}
    oid, _ = ensure_objective(b, "rob", stream)
    assert b.set_objective_status(oid, "paused", user_id="rob")
    oid2, msg = ensure_objective(b, "rob", stream)
    assert oid2 == oid and "paused" in msg
    assert len(b.objectives(user_id="rob")) == 1
