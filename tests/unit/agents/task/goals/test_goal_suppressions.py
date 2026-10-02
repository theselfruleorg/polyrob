"""034 §3.5/§11.1 — what the owner switched OFF stays off.

One table (`goal_suppressions`), three read sites (stream seeding, board.create,
the planner prompt), `/goal cancel` = never again (`--once` = this run only),
`/goal allow` revokes, and `/goals` shows an OFF section.
"""
import sqlite3
import types

import pytest

from agents.task.goals.board import GoalBoard, SuppressedGoalError
from core import goal_suppressions as gs


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    for k in ("STREAM_SEEDING_PAUSE", "DATA_ROOT"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _board(home):
    from core.runtime_paths import goals_db_path
    return GoalBoard(goals_db_path(str(home)))


def _tables(db):
    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


# --- the store ---------------------------------------------------------------

def test_a_read_never_creates_the_table(home):
    b = _board(home)
    assert b.suppressions(user_id="rob") == []
    assert gs.find(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE, value="x") is None
    assert "goal_suppressions" not in _tables(b.db_path)


def test_suppress_is_tenant_scoped_and_normalised(home):
    b = _board(home)
    gs.suppress(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE,
                value="Treasury: publish the track record!", reason="stop posting")
    assert gs.find(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE,
                   value="treasury publish the TRACK record") is not None
    assert gs.find(b.db_path, user_id="other", scope=gs.SCOPE_TITLE,
                   value="Treasury: publish the track record!") is None
    [s] = b.suppressions(user_id="rob")
    assert s.reason == "stop posting" and "publish the track record" in s.label


def test_expired_suppression_is_not_in_force(home):
    b = _board(home)
    gs.suppress(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE, value="t",
                expires_at=100.0, now=50.0)
    assert gs.find(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE, value="t", now=99) is not None
    assert gs.find(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE, value="t", now=101) is None


# --- read site 2: board.create -----------------------------------------------

def test_create_refuses_a_suppressed_title_even_with_force(home):
    b = _board(home)
    gs.suppress(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE, value="Post to the den")
    for force in (False, True):
        with pytest.raises(SuppressedGoalError) as ei:
            b.create(user_id="rob", title="post to the DEN", force=force)
        assert "/goal allow" in str(ei.value)
    b.create(user_id="other", title="Post to the den")          # another tenant is free
    assert gs.allow(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE, value="Post to the den")
    assert b.create(user_id="rob", title="Post to the den").id


def test_create_refuses_under_a_suppressed_stream(home):
    b = _board(home)
    gs.suppress(b.db_path, user_id="rob", scope=gs.SCOPE_STREAM, value="treasury-trading")
    with pytest.raises(SuppressedGoalError):
        b.create(user_id="rob", title="anything", payload={"stream": "treasury-trading"},
                 force=True)


# --- read site 1: stream seeding ---------------------------------------------

def _stream():
    return {"id": "s", "cadence_hours": 1, "objective": {"title": "Mission", "body": "b"},
            "goals": [{"title": "leg one", "body": "a", "tools": ["filesystem"]},
                      {"title": "leg two", "body": "b", "tools": ["filesystem"]},
                      {"title": "leg three", "body": "c", "tools": ["filesystem"]}]}


def test_a_suppressed_leg_is_skipped_and_the_chain_holds(home):
    from agents.task.goals.streams import ensure_objective, seed_stream, stream_is_due
    b = _board(home)
    oid, _ = ensure_objective(b, "rob", _stream())
    gs.suppress(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE, value="leg two")
    assert stream_is_due(b, "rob", _stream())[0] is True     # two legs still on
    created = seed_stream(b, "rob", _stream(), oid)
    assert [g.title for g in created] == ["leg one", "leg three"]
    assert b.dependencies(created[1].id) == [created[0].id]


def test_stream_not_due_when_switched_off(home):
    from agents.task.goals.streams import stream_is_due
    b = _board(home)
    gs.suppress(b.db_path, user_id="rob", scope=gs.SCOPE_STREAM, value="s")
    due, why = stream_is_due(b, "rob", _stream())
    assert due is False and "switched off" in why


def test_stream_not_due_when_every_leg_is_off(home):
    from agents.task.goals.streams import stream_is_due
    b = _board(home)
    for t in ("leg one", "leg two", "leg three"):
        gs.suppress(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE, value=t)
    due, why = stream_is_due(b, "rob", _stream())
    assert due is False and "every leg" in why


# --- read site 3: the planner --------------------------------------------------

def test_planner_prompt_names_what_is_off(home):
    from agents.task.goals.planner import build_planner_prompt
    b = _board(home)
    gs.suppress(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE,
                value="Degen watchlist beyond Base", reason="STOP the watchlist work")
    prompt = build_planner_prompt(b, "rob", None)
    assert "SUPPRESSED (the owner said never again" in prompt
    assert "Degen watchlist beyond Base" in prompt and "STOP the watchlist work" in prompt


# --- the owner verbs (Telegram + REPL share goal_reply) -----------------------

def test_goal_cancel_means_never_again(home):
    from surfaces.telegram import owner_ops
    b = _board(home)
    g = b.create(user_id="rob", title="Publish the track record")
    out = owner_ops.goal_reply("rob", str(home), ["cancel", g.id[:8], "not", "again"])
    assert "switched OFF" in out and "/goal allow" in out
    assert _board(home).get(g.id).status == "cancelled"
    [s] = _board(home).suppressions(user_id="rob")
    assert s.reason == "not again"
    with pytest.raises(SuppressedGoalError):
        _board(home).create(user_id="rob", title="Publish the track record", force=True)


def test_goal_cancel_once_is_just_this_run(home):
    from surfaces.telegram import owner_ops
    b = _board(home)
    g = b.create(user_id="rob", title="Publish the track record")
    out = owner_ops.goal_reply("rob", str(home), ["cancel", g.id[:8], "--once"])
    assert "→ cancelled" in out and "switched OFF" not in out
    assert _board(home).suppressions(user_id="rob") == []


def test_goal_allow_by_id_and_by_title(home):
    from surfaces.telegram import owner_ops
    b = _board(home)
    g = b.create(user_id="rob", title="Publish the track record")
    owner_ops.goal_reply("rob", str(home), ["cancel", g.id[:8]])
    out = owner_ops.goal_reply("rob", str(home), ["allow", g.id[:8]])
    assert "Back ON" in out and _board(home).suppressions(user_id="rob") == []
    g2 = _board(home).create(user_id="rob", title="Refresh the watchlist")
    owner_ops.goal_reply("rob", str(home), ["cancel", g2.id[:8]])
    out = owner_ops.goal_reply("rob", str(home), ["allow", "refresh", "the", "watch"])
    assert "Back ON" in out
    assert "Nothing switched off" in owner_ops.goal_reply("rob", str(home), ["allow", "zzz"])


def test_cancelling_an_already_cancelled_goal_still_switches_it_off(home):
    from surfaces.telegram import owner_ops
    b = _board(home)
    g = b.create(user_id="rob", title="Old thing")
    b.cancel(g.id, user_id="rob")
    out = owner_ops.goal_reply("rob", str(home), ["cancel", g.id[:8]])
    assert "switched OFF" in out
    assert len(_board(home).suppressions(user_id="rob")) == 1


@pytest.mark.asyncio
async def test_repl_goal_is_the_same_helper(home, monkeypatch):
    from cli.ui.commands import h_goal_one
    b = _board(home)
    g = b.create(user_id="rob", title="REPL cancel")
    monkeypatch.setattr(h_goal_one, "_tenant", lambda ctx: "rob")
    monkeypatch.setattr(h_goal_one, "_admin_data_dir", lambda write=False: str(home))
    seen = []
    ctx = types.SimpleNamespace(args=["cancel", g.id[:8]],
                                emit=lambda text, title=None: seen.append(text))
    await h_goal_one.h_goal(ctx)
    assert "switched OFF" in seen[0]


# --- /goals shows what is OFF ----------------------------------------------------

def test_render_board_has_an_off_section(home):
    from core.goal_board_render import render_board
    b = _board(home)
    b.create(user_id="rob", title="Still on")
    gs.suppress(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE, value="Den posts",
                reason="stop posting to the den")
    out = render_board(b, user_id="rob")
    assert "OFF — 1" in out and "Den posts" in out and "/goal allow" in out


def test_render_board_off_section_survives_an_empty_board(home):
    from core.goal_board_render import render_board
    b = _board(home)
    gs.suppress(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE, value="Den posts")
    out = render_board(b, user_id="rob")
    assert out.startswith("No goals yet.") and "Den posts" in out


def test_repl_goals_view_has_an_off_section(home, monkeypatch):
    from cli.ui.commands import h_goals_view
    b = _board(home)
    b.create(user_id="rob", title="Still on")
    gs.suppress(b.db_path, user_id="rob", scope=gs.SCOPE_TITLE, value="Den posts")
    monkeypatch.setattr(h_goals_view, "_board_path", lambda: b.db_path)
    out = h_goals_view.goals_view("rob")
    assert "off (1)" in out and "Den posts" in out
