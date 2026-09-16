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


def test_stream_is_not_due_when_its_objective_is_paused(tmp_path, monkeypatch):
    """034 §11.4: `/goal objective pause` must actually stop the stream.

    `ensure_objective` only refused to CREATE a duplicate objective; it returned
    the paused row's id and `scripts/seed_streams.py` seeded under it, because
    `stream_is_due` never read the objective's status. The owner's only
    per-stream off switch was a no-op.
    """
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.streams import ensure_objective, stream_is_due
    b = GoalBoard(str(tmp_path / "goals.db"))
    stream = {"id": "s", "cadence_hours": 1, "goals": [{"title": "t"}],
              "objective": {"title": "Mission", "body": "b"}}

    oid, _ = ensure_objective(b, "rob", stream)
    assert stream_is_due(b, "rob", stream)[0] is True

    assert b.set_objective_status(oid, "paused", user_id="rob")
    due, why = stream_is_due(b, "rob", stream)
    assert due is False, "a paused objective must stop its stream from seeding"
    assert "paused" in why and "owner" in why

    assert b.set_objective_status(oid, "active", user_id="rob")
    assert stream_is_due(b, "rob", stream)[0] is True, "resuming must re-arm the stream"


def test_stream_objective_active_literal_is_pinned_to_the_board_enum():
    """`streams._OBJ_ACTIVE` is a literal; it must equal the board's own constant."""
    from agents.task.goals.board import OBJ_ACTIVE
    from agents.task.goals import streams
    assert streams._OBJ_ACTIVE == OBJ_ACTIVE


def test_a_dropped_objective_also_stops_its_stream(tmp_path, monkeypatch):
    """Only `active` seeds — `dropped`/`done` must stop the stream too, not just
    `paused`. A dropped objective whose stream kept seeding is the same bug."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.streams import ensure_objective, stream_is_due
    b = GoalBoard(str(tmp_path / "goals.db"))
    stream = {"id": "s", "cadence_hours": 1, "goals": [{"title": "t"}],
              "objective": {"title": "Mission", "body": "b"}}
    oid, _ = ensure_objective(b, "rob", stream)
    assert b.set_objective_status(oid, "dropped", user_id="rob")
    due, why = stream_is_due(b, "rob", stream)
    assert due is False and "dropped" in why


def test_a_stream_with_no_objective_yet_is_still_due(tmp_path, monkeypatch):
    """Fail-open: before `ensure_objective` runs there is no objective row, and the
    new throttle must not deadlock a brand-new stream's first ever seed."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.streams import stream_is_due
    b = GoalBoard(str(tmp_path / "goals.db"))
    stream = {"id": "brand-new", "cadence_hours": 1, "goals": [{"title": "t"}],
              "objective": {"title": "Never seeded", "body": "b"}}
    assert stream_is_due(b, "rob", stream)[0] is True
