from agents.task.goals.board import GoalBoard
from agents.task.goals.context import build_goal_run_task, extract_outcome_line


def _mk(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    o = b.create_objective(user_id="rob", title="Get 100k followers on X",
                           body="X is distribution for substack posts.")
    g = b.create(user_id="rob", title="Draft memory-arch thread", parent_id=o.id,
                 body="Draft a 5-tweet thread.",
                 payload={"acceptance": "a draft file under project/drafts/"})
    return b, o, g


def test_task_contains_objective_body_acceptance_outcome(tmp_path):
    b, o, g = _mk(tmp_path)
    t = build_goal_run_task(g, o)
    assert "STANDING OBJECTIVE" in t and "Get 100k followers on X" in t
    assert "Draft a 5-tweet thread." in t
    assert "Definition of done" in t and "project/drafts/" in t
    assert "OUTCOME:" in t


def test_task_without_objective(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="One-off", body="Do a thing.")
    t = build_goal_run_task(g, None)
    assert "one-off goal" in t.lower() and "Do a thing." in t


def test_extract_outcome_line():
    text = "did stuff\nmore\nOUTCOME: posted tweet 123, wrote project/a.md"
    assert extract_outcome_line(text) == "posted tweet 123, wrote project/a.md"
    assert extract_outcome_line("no outcome here") is None
    assert extract_outcome_line(None) is None


def test_set_outcome_on_done_goal(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="t goal")
    b.claim(g.id, "w", ttl_seconds=60)
    b.record_success(g.id, result="OUTCOME: x")
    assert b.set_outcome(g.id, "x") is True
    assert b.get(g.id).payload["outcome"] == "x"
