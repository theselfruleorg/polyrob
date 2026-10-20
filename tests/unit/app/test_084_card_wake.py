"""084 WS3: the drain runs a settled card group's wake.

A chat group wakes its session once with the outcomes; a live owner turn makes
it wait (no attempt spent); a group a turn already showed is dropped; a goal
group unblocks the goal and wakes nobody; a cron group runs nothing early.
"""
import asyncio
import time

import pytest

from core.surfaces import card_groups as G
from core.surfaces import cards
from polyrob.app import card_wake

U, S = "owner1", "sess-chat"
LINE = ["24", "0x" + "ab" * 20, "to", "0x" + "cd" * 20, "on", "robinhood"]


@pytest.fixture
def st(tmp_path):
    store = cards.CardStore(str(tmp_path / "cards.db"))
    cards.set_store(store)
    G._GROUPS.clear()
    yield store
    cards.set_store(None)
    G._GROUPS.clear()


class _Agent:
    def __init__(self, ok=True, pending=False):
        self.ok, self.pending, self.calls = ok, pending, []

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.calls.append((session_id, user_id, text, metadata))
        if isinstance(self.ok, Exception):
            raise self.ok
        return self.ok

    def _session_has_pending_input(self, session_id):
        return self.pending


def _settled_group(st, origin=G.ORIGIN_CHAT, ref="", session=S):
    gid = G.groups(st).open_group_for(U, session, origin=origin, origin_ref=ref)
    p = cards.proposal_card(U, "/send", LINE, session_id=session, group_id=gid)
    cards.press(p.card_id, "no", U)
    assert G.groups(st).get(gid)["state"] == G.G_SETTLED
    return gid


def _run(gid, agent):
    return asyncio.run(card_wake.run_card_wake({"group_id": gid}, U, agent))


def test_a_chat_group_wakes_its_session_once(st, monkeypatch):
    monkeypatch.setattr("core.interactive_gate.read_turn_marker", lambda: None)
    gid = _settled_group(st)
    agent = _Agent()
    assert _run(gid, agent) is True
    (sid, uid, text, meta), = agent.calls
    assert sid == S and "DISMISSED" in text
    assert meta["source"] == G.WAKE_KIND
    assert G.groups(st).get(gid)["state"] == G.G_WOKEN
    assert _run(gid, agent) is True and len(agent.calls) == 1      # never twice


def test_a_live_owner_turn_makes_the_wake_wait(st, monkeypatch):
    monkeypatch.setattr("core.interactive_gate.read_turn_marker",
                        lambda: {"session_id": S, "kind": "owner_chat"})
    gid = _settled_group(st)
    agent = _Agent()
    assert _run(gid, agent) is None and agent.calls == []
    assert G.groups(st).get(gid)["state"] == G.G_SETTLED


def test_queued_owner_input_makes_the_wake_wait(st, monkeypatch):
    monkeypatch.setattr("core.interactive_gate.read_turn_marker", lambda: None)
    gid = _settled_group(st)
    assert _run(gid, _Agent(pending=True)) is None


def test_a_group_the_owner_turn_already_showed_is_dropped(st, monkeypatch):
    monkeypatch.setattr("core.interactive_gate.read_turn_marker", lambda: None)
    gid = _settled_group(st)
    assert G.take_outcomes(session_id=S, user_id=U, st=st)
    agent = _Agent()
    assert _run(gid, agent) is True and agent.calls == []


def test_a_refused_wake_returns_the_group_to_settled_for_a_retry(st, monkeypatch):
    monkeypatch.setattr("core.interactive_gate.read_turn_marker", lambda: None)
    gid = _settled_group(st)
    assert _run(gid, _Agent(ok=False)) is False
    assert G.groups(st).get(gid)["state"] == G.G_SETTLED


def test_a_goal_group_unblocks_the_goal_and_wakes_nobody(st, monkeypatch):
    seen = []

    class _Board:
        def __init__(self, path):
            seen.append(path)

        def unblock(self, goal_id, *, user_id, rationale=""):
            seen.append((goal_id, user_id))
            return True

    monkeypatch.setattr("agents.task.goals.board.GoalBoard", _Board)
    goals = st.db_path.replace("cards.db", "goals.db")
    open(goals, "w").close()
    gid = _settled_group(st, origin=G.ORIGIN_GOAL, ref="g123", session="sess-goal")
    agent = _Agent()
    assert _run(gid, agent) is True and agent.calls == []
    assert ("g123", U) in seen
    assert G.groups(st).get(gid)["state"] == G.G_SETTLED    # its next run shows it


def test_a_cron_group_runs_nothing_early(st):
    gid = _settled_group(st, origin=G.ORIGIN_CRON, ref="job9", session="sess-cron")
    agent = _Agent()
    assert _run(gid, agent) is True and agent.calls == []
    text = G.take_outcomes(session_id="sess-cron-2", user_id=U, origin=G.ORIGIN_CRON,
                           origin_ref="job9", st=st)
    assert "DISMISSED" in text


def test_the_wake_text_says_the_owner_acted():
    from agents.task.agent.core.self_wake import format_self_wake
    body = format_self_wake("- /send 24 … → DONE", source="cards_settled")
    assert body.startswith("[internal trigger] The owner has decided every card")
    assert "background job you started" not in body
    assert "untrusted" in body
    assert format_self_wake("x").startswith("[internal trigger] A background job")


def test_the_drain_registers_the_runner():
    from core.approved_actions import runner_for
    card_wake.register()
    assert runner_for({"kind": G.WAKE_KIND}) is card_wake.run_card_wake


@pytest.fixture
def idle(monkeypatch):
    monkeypatch.setattr("core.interactive_gate.read_turn_marker", lambda: None)
    monkeypatch.setattr("core.interactive_gate.is_interactive_busy", lambda: False)


def test_a_wake_that_raises_returns_the_group_for_a_retry(st, idle):
    gid = _settled_group(st)
    assert _run(gid, _Agent(ok=RuntimeError("budget"))) is False
    assert G.groups(st).get(gid)["state"] == G.G_SETTLED


def test_a_busy_seat_makes_the_wake_wait(st, monkeypatch):
    monkeypatch.setattr("core.interactive_gate.read_turn_marker", lambda: None)
    monkeypatch.setattr("core.interactive_gate.is_interactive_busy", lambda: True)
    assert _run(_settled_group(st), _Agent()) is None


def test_the_wake_waits_for_the_rest_of_the_batch_then_wakes_once(st, idle):
    first = _settled_group(st)
    gid2 = G.groups(st).open_group_for(U, S)
    p = cards.proposal_card(U, "/send", LINE, session_id=S, group_id=gid2)
    agent = _Agent()
    assert _run(first, agent) is None and agent.calls == []
    cards.press(p.card_id, "no", U)
    assert _run(first, agent) is True
    (_, _, text, _), = agent.calls
    assert text.count("DISMISSED") == 2
    assert {G.groups(st).get(x)["state"] for x in (first, gid2)} == {G.G_WOKEN}
    assert _run(gid2, agent) is True and len(agent.calls) == 1


def test_the_sweep_never_creates_a_cards_db(tmp_path, monkeypatch):
    cards.set_store(None)
    monkeypatch.setattr("core.runtime_paths.data_home_db_path",
                        lambda name, **kw: str(tmp_path / name))
    monkeypatch.setattr(card_wake, "_last_sweep", 0.0)
    assert card_wake.maybe_sweep() == 0
    assert not (tmp_path / "cards.db").exists()


def test_a_woken_run_that_ends_well_consumes_its_groups(st):
    gid = _settled_group(st)
    G.groups(st).transition(gid, frozenset({G.G_SETTLED}), G.G_WOKEN)
    G.close_woken([gid], True, st=st)
    assert G.groups(st).get(gid)["state"] == G.G_CONSUMED


def test_a_woken_run_that_failed_puts_its_groups_back(st):
    gid = _settled_group(st)
    G.groups(st).transition(gid, frozenset({G.G_SETTLED}), G.G_WOKEN)
    G.close_woken([gid], False, st=st)
    assert "DISMISSED" in G.take_outcomes(session_id=S, user_id=U, st=st)


def test_a_stale_woken_group_shows_on_the_next_turn(st):
    gid = _settled_group(st)
    G.groups(st).transition(gid, frozenset({G.G_SETTLED}), G.G_WOKEN)
    assert G.take_outcomes(session_id=S, user_id=U, st=st) == ""      # fresh: its run owns it
    conn = st._conn()
    conn.execute("UPDATE card_groups SET updated_at=? WHERE group_id=?",
                 (time.time() - G.WOKEN_STALE_S - 5, gid))
    conn.commit()
    conn.close()
    assert "DISMISSED" in G.take_outcomes(session_id=S, user_id=U, st=st)
    assert G.groups(st).get(gid)["state"] == G.G_CONSUMED


def _delivery_agent(status, seen):
    from agents.task.task_agent_delivery import TaskAgentDeliveryMixin

    class _A(TaskAgentDeliveryMixin):
        pass

    a = _A()
    info = {"status": status, "request": {"task": "pay the raffle winners", "max_steps": 99}}

    class SM:
        def get_session_info(self, sid):
            return info

        def update_session_request(self, sid, req):
            info["request"] = req

    class Reg:
        def get(self, sid):
            return None

    a.session_manager, a._registry = SM(), Reg()

    async def _run(user_id, session_id):
        seen.append(info["request"]["task"])

    a.run_session = _run
    return a, info


def test_a_finished_chat_woken_for_cards_continues_without_rerunning(st):
    seen = []
    a, info = _delivery_agent("completed", seen)
    a._narrow_finished_session_task(S, info, "cards_settled")
    task = info["request"]["task"]
    assert task.startswith("[cards-reply]") and "Do not repeat a step whose card is DONE" in task
    assert "pay the raffle winners" in task          # context, after the guard
    assert info["request"]["original_task"] == "pay the raffle winners"


def test_run_woken_closes_the_groups_by_run_status(st):
    gid = _settled_group(st)
    G.groups(st).transition(gid, frozenset({G.G_SETTLED}), G.G_WOKEN)
    a, _ = _delivery_agent("failed", [])
    asyncio.run(a._run_woken(U, S, card_groups=[gid]))
    assert G.groups(st).get(gid)["state"] == G.G_SETTLED
