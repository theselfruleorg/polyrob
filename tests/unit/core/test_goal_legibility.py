"""034 §3.1/§3.2/§7 — the provenance axis, the status collapse and the stamp gap."""
import types

import pytest

from core.goal_legibility import (
    bucket_counts, bucket_line, origin, origin_counts, origin_tag, owner_bucket,
)


def _g(**payload):
    return types.SimpleNamespace(payload=payload)


@pytest.mark.parametrize("payload,want", [
    ({"owner_granted": True, "stream": "x"}, "OWNER"),        # /trade wins
    ({"stream": "treasury-trading"}, "GRANTED"),
    ({"cycle": "legacy-tag"}, "GRANTED"),
    ({"origin_session_id": "s1", "created_by_session_id": "s1",
      "authored_by": "owner"}, "ASKED"),                       # genuine owner chat turn
    ({"authored_by": "owner"}, "OWNER"),                       # an owner seat
    ({"created_by_session_id": "s9", "authored_by": "agent"}, "SELF"),
    ({"created_by_session_id": "s9"}, "SELF"),
    ({}, "LEGACY"),
])
def test_origin_is_derived_from_existing_keys(payload, want):
    assert origin(_g(**payload)) == want


def test_origin_tags_read_as_the_owner_would():
    assert origin_tag(_g(stream="ship-software")) == "[granted: ship-software]"
    assert origin_tag(_g(created_by_session_id="s")) == "[I made this one up]"
    assert origin_counts([_g(), _g(stream="a"), _g(stream="b")]) == {"LEGACY": 1, "GRANTED": 2}


def test_the_status_collapse_is_a_projection():
    assert owner_bucket("running") == "NOW"
    assert {owner_bucket(s) for s in ("triage", "waiting", "ready")} == {"NEXT"}
    assert owner_bucket("blocked") == "STUCK"
    assert {owner_bucket(s) for s in ("done", "cancelled")} == {"OVER"}
    assert owner_bucket("obsolete") == "OTHER"          # tolerated, never assumed
    counts = {"done": 490, "cancelled": 74, "ready": 2, "blocked": 2, "obsolete": 1}
    assert bucket_counts(counts) == {"STUCK": 2, "NEXT": 2, "OVER": 564, "OTHER": 1}
    assert bucket_line(counts) == "STUCK 2 · NEXT 2 · OVER 564 · OTHER 1"


def test_owner_seat_creates_are_stamped(tmp_path):
    """§7 stamp gap: owner-seat creates carried no provenance -> LEGACY."""
    from agents.task.goals.board import GoalBoard
    from core.owner_create import create_goal
    b = GoalBoard(str(tmp_path / "goals.db"))
    g = create_goal(b, user_id="rob", title="From the console")
    assert origin(g) == "OWNER"


def test_cli_goals_create_is_stamped(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from agents.task.goals.board import GoalBoard
    import cli.commands.goals as goals_cmd
    b = GoalBoard(str(tmp_path / "goals.db"))
    monkeypatch.setattr(goals_cmd, "_get_board", lambda: b)
    monkeypatch.setattr(goals_cmd, "_owner_tenant", lambda: "rob")
    r = CliRunner().invoke(goals_cmd.goals, ["create", "From the terminal"])
    assert r.exit_code == 0, r.output
    [g] = b.list_recent(user_id="rob", limit=5)
    assert origin(g) == "OWNER"
