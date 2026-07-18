"""Telegram owner-admin verbs (§7.1 missing hop + §7.2b): /pending /approve /reject
/asks /fulfill — the phone-only headless owner's approve surface.

Owner-gated by principal (network surface → NO local bypass); a non-owner gets a
refusal and the primitives are never touched.
"""
import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind, _COMMANDS
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.harness import act_on_inbound
from surfaces.telegram.inbound import InboundResult


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


def _cmd(command, text, user="gleb"):
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(text=text,
                             identity=Identity(user_id=user, source=src, raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:dm:555:" + user, command=command))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "gleb")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    return tmp_path


def _seed_pending_self(home, uid="gleb"):
    from core.self_context_writer import PROVENANCE_AGENT, SelfContextWriter
    SelfContextWriter(home, instance_id="rob").propose(
        "Learned: escalate blockers to the owner proactively.",
        user_id=uid, created_by=PROVENANCE_AGENT, pending=True)


def test_new_verbs_are_routable_commands():
    for verb in ("/pending", "/approve", "/reject", "/asks", "/fulfill"):
        assert verb in _COMMANDS


@pytest.mark.asyncio
async def test_pending_lists_for_owner(env):
    _seed_pending_self(env)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/pending", "/pending"))
    assert "escalate blockers" in out
    assert "self_context" in out


@pytest.mark.asyncio
async def test_pending_empty_for_owner(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/pending", "/pending"))
    assert "no pending" in out.lower()


@pytest.mark.asyncio
async def test_pending_refused_for_non_owner(env):
    _seed_pending_self(env)
    out = await act_on_inbound(_Agent(str(env)),
                               _cmd("/pending", "/pending", user="u_stranger"))
    assert "owner" in out.lower()
    assert "escalate blockers" not in out


@pytest.mark.asyncio
async def test_approve_promotes_self_context(env):
    from core.instance import load_self_doc
    _seed_pending_self(env)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/approve", "/approve gleb"))
    assert "promoted" in out.lower()
    assert "escalate blockers" in load_self_doc(env, user_id="gleb")


@pytest.mark.asyncio
async def test_reject_archives_self_context(env):
    _seed_pending_self(env)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/reject", "/reject gleb"))
    assert "rejected" in out.lower()
    out2 = await act_on_inbound(_Agent(str(env)), _cmd("/pending", "/pending"))
    assert "no pending" in out2.lower()


@pytest.mark.asyncio
async def test_approve_unknown_id(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/approve", "/approve nope"))
    assert "no pending" in out.lower() or "not found" in out.lower()


@pytest.mark.asyncio
async def test_asks_and_fulfill_roundtrip(env):
    from agents.task.goals.board import ASK_FULFILLED, GoalBoard
    import os
    board = GoalBoard(os.path.join(str(env), "goals.db"))
    a = board.create_ask(user_id="gleb", what="Grant Twitter write access",
                         why="X objective needs twitter_post")
    agent = _Agent(str(env))
    out = await act_on_inbound(agent, _cmd("/asks", "/asks"))
    assert "Grant Twitter write access" in out
    assert a.id in out
    out2 = await act_on_inbound(agent, _cmd("/fulfill", f"/fulfill {a.id}"))
    assert "fulfilled" in out2.lower()
    assert board.get(a.id).status == ASK_FULFILLED


@pytest.mark.asyncio
async def test_fulfill_refused_for_non_owner(env):
    from agents.task.goals.board import ASK_OPEN, GoalBoard
    import os
    board = GoalBoard(os.path.join(str(env), "goals.db"))
    a = board.create_ask(user_id="gleb", what="Grant Twitter write access")
    out = await act_on_inbound(_Agent(str(env)),
                               _cmd("/fulfill", f"/fulfill {a.id}", user="u_stranger"))
    assert "owner" in out.lower()
    assert board.get(a.id).status == ASK_OPEN


@pytest.mark.asyncio
async def test_help_mentions_owner_verbs(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/help", "/help"))
    assert "/pending" in out and "/asks" in out


# --- Task 9 / G-2: tool-approval asks ride the SAME /pending /approve /reject verbs,
# namespaced tap-<id> so they never collide with a self-evolution proposal id. ---------

def _seed_tool_approval(home, uid="gleb", tool_name="x402_request"):
    from agents.task.goals.board import GoalBoard
    import os
    board = GoalBoard(os.path.join(str(home), "goals.db"))
    ask = board.create_ask(
        user_id=uid, what=f"Approve {tool_name}? [deadbeef01]",
        why=f"tool={tool_name} params={{}}",
        extra_payload={"ask_kind": "tool_approval", "tool_name": tool_name,
                       "request_hash": "deadbeef01", "grant_consumed": False},
        force=True,
    )
    return board, ask


@pytest.mark.asyncio
async def test_pending_lists_tool_approval_ask(env):
    board, ask = _seed_tool_approval(env)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/pending", "/pending"))
    assert "tool_approval" in out
    assert f"tap-{ask.id}" in out


@pytest.mark.asyncio
async def test_approve_resolves_tool_approval_ask(env):
    from agents.task.goals.board import ASK_FULFILLED
    board, ask = _seed_tool_approval(env)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/approve", f"/approve tap-{ask.id}"))
    assert "approved" in out.lower()
    assert board.get(ask.id).status == ASK_FULFILLED
    assert board.get(ask.id).payload["decision"] == "approved"


@pytest.mark.asyncio
async def test_reject_resolves_tool_approval_ask(env):
    from agents.task.goals.board import ASK_REJECTED
    board, ask = _seed_tool_approval(env)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/reject", f"/reject tap-{ask.id}"))
    assert "rejected" in out.lower()
    assert board.get(ask.id).status == ASK_REJECTED


@pytest.mark.asyncio
async def test_approve_tool_approval_unknown_id(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/approve", "/approve tap-nope"))
    assert "no open" in out.lower() or "failed" in out.lower()


@pytest.mark.asyncio
async def test_approve_still_dispatches_self_evolution_when_no_tap_prefix(env):
    """Namespace dispatch must not break the existing self-evolution /approve —
    a bare (non tap-) id still routes to core.self_evolution."""
    _seed_pending_self(env)
    _seed_tool_approval(env)  # a tool_approval ask also open at the same time
    out = await act_on_inbound(_Agent(str(env)), _cmd("/approve", "/approve gleb"))
    assert "promoted" in out.lower()


@pytest.mark.asyncio
async def test_pending_refused_for_non_owner_hides_tool_approval(env):
    _seed_tool_approval(env)
    out = await act_on_inbound(_Agent(str(env)),
                               _cmd("/pending", "/pending", user="u_stranger"))
    assert "owner" in out.lower()
    assert "tool_approval" not in out


@pytest.mark.asyncio
async def test_asks_excludes_tool_approval_asks(env):
    """A tool_approval ask has its OWN surface (/pending) — /asks (the generic
    ask board) must not also list it under a second, un-namespaced id."""
    board, ask = _seed_tool_approval(env)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/asks", "/asks"))
    assert "no open asks" in out.lower()
    assert ask.id not in out
