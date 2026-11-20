"""120 W2: goal_pause and goal_unblock on a paused goal (owner turn only, the owner
seats' rule in core.goal_owner_verbs), and goal_list(kind='ask')."""
import types

import pytest

from modules.goals.board import GoalBoard
from core import autonomy_marker
from tools import goal_tools as T

OWNER = "alice"


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: OWNER)
    yield
    autonomy_marker._SESSIONS.pop("s-run", None)


@pytest.fixture
def tool(tmp_path):
    t = T.GoalTool.__new__(T.GoalTool)
    t._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return t, t._goal_board


def _owner_turn():
    return types.SimpleNamespace(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                                 metadata={}, session_id="s-owner", parent_session_id=None)


def _run_turn(goal_id):
    autonomy_marker.mark_autonomous("s-run", goal_id)
    return types.SimpleNamespace(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                                 metadata={}, session_id="s-run", parent_session_id=None)


@pytest.mark.asyncio
async def test_goal_pause_on_an_owner_turn_uses_the_seat_rule(tool):
    t, board = tool
    g = board.create(user_id=OWNER, title="weekly report")
    res = await t.goal_pause(T.GoalPauseAction(goal_id=g.id), execution_context=_owner_turn())
    assert not res.error and board.get(g.id).status == "paused"
    assert f"goal_unblock(goal_id='{g.id}')" in res.extracted_content
    assert "/goal retry" not in res.extracted_content


@pytest.mark.asyncio
async def test_goal_pause_is_refused_off_an_owner_turn(tool):
    t, board = tool
    g = board.create(user_id=OWNER, title="weekly report")
    other = board.create(user_id=OWNER, title="another goal")
    res = await t.goal_pause(T.GoalPauseAction(goal_id=g.id),
                             execution_context=_run_turn(other.id))
    assert res.error and board.get(g.id).status == "ready"


@pytest.mark.asyncio
async def test_goal_pause_on_the_goal_the_run_runs_in_is_refused(tool):
    t, board = tool
    g = board.create(user_id=OWNER, title="weekly report")
    res = await t.goal_pause(T.GoalPauseAction(goal_id=g.id), execution_context=_run_turn(g.id))
    assert res.error and "cannot change the goal it is running" in res.error
    assert board.get(g.id).status == "ready"


@pytest.mark.asyncio
async def test_unblock_lifts_a_pause_on_an_owner_turn_only(tool):
    t, board = tool
    g = board.create(user_id=OWNER, title="weekly report")
    board.update_status(g.id, "paused", user_id=OWNER)
    other = board.create(user_id=OWNER, title="another goal")
    res = await t.goal_unblock(T.GoalUnblockAction(goal_id=g.id),
                               execution_context=_run_turn(other.id))
    assert res.error and board.get(g.id).status == "paused"
    res = await t.goal_unblock(T.GoalUnblockAction(goal_id=g.id),
                               execution_context=_owner_turn())
    assert not res.error and board.get(g.id).status == "ready"
    assert "pause lifted" in res.extracted_content


@pytest.mark.asyncio
async def test_unblock_of_a_paused_goal_with_an_unfinished_prerequisite_waits(tool):
    t, board = tool
    first = board.create(user_id=OWNER, title="collect the data")
    g = board.create(user_id=OWNER, title="write the report", depends_on=[first.id])
    board.update_status(g.id, "paused", user_id=OWNER)
    res = await t.goal_unblock(T.GoalUnblockAction(goal_id=g.id),
                               execution_context=_owner_turn())
    assert not res.error and board.get(g.id).status == "waiting"


@pytest.mark.asyncio
async def test_unblock_of_a_blocked_goal_keeps_its_old_behaviour(tool):
    """A standing run may still requeue ANOTHER blocked goal, as before W2."""
    t, board = tool
    g = board.create(user_id=OWNER, title="weekly report")
    board.update_status(g.id, "blocked", user_id=OWNER)
    other = board.create(user_id=OWNER, title="another goal")
    ctx = _run_turn(other.id)
    import core.security.owner_turn as OT
    orig = OT.is_owner_standing_run
    OT.is_owner_standing_run = lambda c: True
    try:
        res = await t.goal_unblock(T.GoalUnblockAction(goal_id=g.id, rationale="fixed"),
                                   execution_context=ctx)
    finally:
        OT.is_owner_standing_run = orig
    assert not res.error and board.get(g.id).status == "ready"
    assert "retry budget reset" in res.extracted_content


# --- asks ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ask_list_shows_question_holds_and_answer(tool):
    t, board = tool
    g = board.create(user_id=OWNER, title="weekly report")
    open_ask = board.create_ask(user_id=OWNER, what="which chain do I report on?",
                                blocks_goal_ids=[g.id])
    done = board.create_ask(user_id=OWNER, what="may I post the summary?", force=True)
    board.decide_ask(done.id, user_id=OWNER, approved=True, answer="yes, on Fridays")
    res = await t.goal_list(T.GoalListAction(kind="ask"), execution_context=_owner_turn())
    assert open_ask.id in res.extracted_content and g.id in res.extracted_content
    assert "which chain do I report on?" in res.extracted_content
    assert done.id not in res.extracted_content
    res = await t.goal_list(T.GoalListAction(kind="ask", status="fulfilled"),
                            execution_context=_owner_turn())
    assert done.id in res.extracted_content and "yes, on Fridays" in res.extracted_content


@pytest.mark.asyncio
async def test_goal_list_default_is_still_goals(tool):
    t, board = tool
    board.create(user_id=OWNER, title="weekly report")
    board.create_ask(user_id=OWNER, what="which chain do I report on?")
    res = await t.goal_list(T.GoalListAction(), execution_context=_owner_turn())
    assert "weekly report" in res.extracted_content
    assert "which chain" not in res.extracted_content


@pytest.mark.asyncio
async def test_an_unreadable_ask_store_is_named_never_no_asks(tool, monkeypatch):
    t, board = tool

    def _boom(**kw):
        raise OSError("disk")

    monkeypatch.setattr(board, "asks", _boom)
    res = await t.goal_list(T.GoalListAction(kind="ask"), execution_context=_owner_turn())
    assert res.error and "could not be read" in res.error
    assert "No open asks" not in (res.extracted_content or "")
