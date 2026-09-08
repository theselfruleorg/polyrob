import json

import pytest
from click.testing import CliRunner

import cli.commands.goals as goals_mod
from agents.task.goals.board import GoalBoard
from cli.commands.goals import goals


@pytest.fixture
def board(tmp_path, monkeypatch):
    b = GoalBoard(str(tmp_path / "goals.db"))
    monkeypatch.setattr(goals_mod, "_get_board", lambda data_root=None: b)
    monkeypatch.setattr("core.identity.resolve_identity", lambda: "rob")
    return b


def test_objective_add_list_pause(board):
    r = CliRunner().invoke(goals, ["objective", "add", "Get 100k followers", "-b", "X distributes."])
    assert r.exit_code == 0, r.output
    oid = board.objectives(user_id="rob")[0].id
    r = CliRunner().invoke(goals, ["objective", "list"])
    assert "Get 100k followers" in r.output
    r = CliRunner().invoke(goals, ["objective", "pause", oid])
    assert r.exit_code == 0
    assert board.get(oid).status == "paused"
    r = CliRunner().invoke(goals, ["objective", "activate", oid])
    assert board.get(oid).status == "active"


def test_create_with_tools_acceptance_objective(board):
    o = board.create_objective(user_id="rob", title="Earn money")
    r = CliRunner().invoke(goals, [
        "create", "Draft pricing post", "--objective", o.id,
        "--tools", "filesystem,task,twitter", "--acceptance", "a tweet id"])
    assert r.exit_code == 0, r.output
    g = board.children(o.id)[0]
    assert g.payload["tools"] == ["filesystem", "task", "twitter"]
    assert g.payload["acceptance"] == "a tweet id"


def test_create_duplicate_shows_match_and_force(board):
    board.create(user_id="rob", title="Research revenue angles for POLYROB")
    r = CliRunner().invoke(goals, ["create", "Research revenue angles for POLYROB"])
    assert r.exit_code == 1 and "duplicate" in r.output.lower()
    r = CliRunner().invoke(goals, ["create", "Research revenue angles for POLYROB", "--force"])
    assert r.exit_code == 0


def test_edit(board):
    g = board.create(user_id="rob", title="Old goal title words")
    r = CliRunner().invoke(goals, ["edit", g.id, "--priority", "9",
                                   "--acceptance", "file project/x.md exists"])
    assert r.exit_code == 0, r.output
    g2 = board.get(g.id)
    assert g2.priority == 9 and g2.payload["acceptance"] == "file project/x.md exists"


def test_tree(board):
    o = board.create_objective(user_id="rob", title="Earn money")
    g = board.create(user_id="rob", title="Draft pricing brief", parent_id=o.id)
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_success(g.id, result="OUTCOME: project/p.md")
    board.set_outcome(g.id, "project/p.md")
    orphan = board.create(user_id="rob", title="Unattached goal words")
    r = CliRunner().invoke(goals, ["tree"])
    assert r.exit_code == 0, r.output
    assert "Earn money" in r.output and "Draft pricing brief" in r.output
    assert "project/p.md" in r.output
    assert "Unattached goal words" in r.output


def test_list_marks_missing_outcome(board):
    g = board.create(user_id="rob", title="A done goal lacking outcome")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_success(g.id, result="ok")
    r = CliRunner().invoke(goals, ["list"])
    assert "[no outcome]" in r.output


def test_objective_add_duplicate_and_force(board):
    board.create(user_id="rob", title="Grow the substack audience")
    r = CliRunner().invoke(goals, ["objective", "add", "Grow the substack audience"])
    assert r.exit_code == 1 and "force" in r.output.lower()
    r = CliRunner().invoke(goals, ["objective", "add", "Grow the substack audience", "--force"])
    assert r.exit_code == 0, r.output


def test_objective_add_accepts_criteria_budget_and_stream_id(board):
    r = CliRunner().invoke(goals, [
        "objective", "add", "Ship weekly video",
        "-b", "One video a week.",
        "--success-criteria", "4 published urls in 4 weeks",
        "--goal-budget", "8",
        "--stream-id", "video-weekly",
    ])
    assert r.exit_code == 0, r.output
    o = board.objectives(user_id="rob")[0]
    assert o.payload["success_criteria"] == "4 published urls in 4 weeks"
    assert o.payload["goal_budget"] == 8
    assert o.payload["stream_id"] == "video-weekly"


def test_objective_show_reports_criteria_and_budget_use(board):
    o = board.create_objective(user_id="rob", title="Ship weekly video",
                               payload={"success_criteria": "4 urls", "goal_budget": 8})
    board.create(user_id="rob", title="child", parent_id=o.id, force=True)
    r = CliRunner().invoke(goals, ["objective", "show", o.id])
    assert r.exit_code == 0, r.output
    assert "4 urls" in r.output
    assert "1/8" in r.output


def test_objective_show_refuses_unknown_id(board):
    r = CliRunner().invoke(goals, ["objective", "show", "nonexistent-id"])
    assert r.exit_code != 0
    assert "no such objective" in r.output


def test_objective_show_refuses_cross_tenant_read(board):
    o = board.create_objective(user_id="someone-else", title="Someone else's stream",
                               force=True)
    r = CliRunner().invoke(goals, ["objective", "show", o.id])
    assert r.exit_code != 0
    assert "no such objective" in r.output


def test_objective_show_labels_the_budget_honestly(board):
    """`children_of` keeps `done` children, so the count is a LIFETIME tally and
    calling it "live" told the operator the wrong thing about what is running."""
    o = board.create_objective(user_id="rob", title="Ship weekly video",
                               payload={"goal_budget": 8})
    finished = board.create(user_id="rob", title="a finished child",
                            parent_id=o.id, force=True)
    board.claim(finished.id, "w", ttl_seconds=60)
    board.record_success(finished.id, result="ok")
    board.create(user_id="rob", title="a running child", parent_id=o.id, force=True)

    r = CliRunner().invoke(goals, ["objective", "show", o.id])
    assert r.exit_code == 0, r.output
    assert "2/8 spent" in r.output
    assert "1 in flight" in r.output


def test_objective_show_says_a_stream_objective_is_uncapped(board):
    """A stream objective is exempt from the lifetime budget, so printing
    "12/12 spent" against a manifest number nothing enforces would be a lie."""
    o = board.create_objective(user_id="rob", title="Trade the treasury",
                               payload={"goal_budget": 2, "stream_id": "treasury-trading"})
    board.create(user_id="rob", title="leg one", parent_id=o.id, force=True)
    board.create(user_id="rob", title="leg two words differ", parent_id=o.id, force=True)
    board.create(user_id="rob", title="leg three entirely other", parent_id=o.id, force=True)

    r = CliRunner().invoke(goals, ["objective", "show", o.id])
    assert r.exit_code == 0, r.output
    assert "uncapped" in r.output
    assert "3 goal(s) in flight" in r.output
