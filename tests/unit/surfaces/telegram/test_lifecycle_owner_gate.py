"""043 A10: `/cancel` and `/new` used to force-cancel a session and drop the
chat binding BEFORE any permission check — `_OWNER_ADMIN_COMMANDS` never
listed them, so ANY sender (owner or stranger, DM or group) could cancel
whatever session happened to be bound to that chat.

The fix is NOT "owner only" — a permitted non-owner DM user must still be
able to cancel or restart THEIR OWN session, since a Telegram private chat is
1:1 with its sender. A group chat's session is shared across every member, so
a non-owner there is always denied — same denial string the DENIED routing
branch uses (`_UNAUTHORIZED_TEXT`), reused rather than duplicated.
"""
import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.harness import _UNAUTHORIZED_TEXT, act_on_inbound
from surfaces.telegram.inbound import InboundResult


class _FakeTaskAgent:
    def __init__(self):
        self.cancelled = []
        self.unbound = []

    async def cancel_session_by_id(self, session_id, force=False):
        self.cancelled.append(session_id)
        return True

    def unbind_chat(self, session_key):
        self.unbound.append(session_key)


def _cmd(command, *, user_id, chat_id, chat_type, session_id="sess_1"):
    source = SessionSource(surface_id="telegram", chat_id=chat_id, chat_type=chat_type)
    inbound = InboundMessage(text=command,
                             identity=Identity(user_id=user_id, source=source,
                                               raw_user_id=chat_id))
    if chat_type == "dm":
        session_key = f"agent:main:telegram:dm:{chat_id}:{user_id}"
    else:
        session_key = f"agent:main:telegram:group:{chat_id}"
    decision = RouteDecision(kind=RouteKind.COMMAND, session_key=session_key,
                             session_id=session_id, command=command)
    return InboundResult(inbound=inbound, decision=decision)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    return None


# ---------------------------------------------------------------------------
# /cancel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_denied_for_non_owner_in_group(env):
    ta = _FakeTaskAgent()
    result = _cmd("/cancel", user_id="stranger", chat_id="999", chat_type="group")
    out = await act_on_inbound(ta, result, spawn=lambda c: None)
    assert out == _UNAUTHORIZED_TEXT
    assert ta.cancelled == []


@pytest.mark.asyncio
async def test_cancel_permitted_for_non_owner_in_own_dm(env):
    ta = _FakeTaskAgent()
    result = _cmd("/cancel", user_id="u_stranger", chat_id="555", chat_type="dm")
    out = await act_on_inbound(ta, result, spawn=lambda c: None)
    assert ta.cancelled == ["sess_1"]
    assert out == "Task cancelled."


@pytest.mark.asyncio
async def test_cancel_permitted_for_owner_in_group(env):
    ta = _FakeTaskAgent()
    result = _cmd("/cancel", user_id="alice", chat_id="999", chat_type="group")
    out = await act_on_inbound(ta, result, spawn=lambda c: None)
    assert ta.cancelled == ["sess_1"]
    assert out == "Task cancelled."


# ---------------------------------------------------------------------------
# /new
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_denied_for_non_owner_in_group_binding_not_dropped(env):
    ta = _FakeTaskAgent()
    result = _cmd("/new", user_id="stranger", chat_id="999", chat_type="group")
    out = await act_on_inbound(ta, result, spawn=lambda c: None)
    assert out == _UNAUTHORIZED_TEXT
    assert ta.cancelled == []
    assert ta.unbound == []


@pytest.mark.asyncio
async def test_new_permitted_for_non_owner_in_own_dm(env):
    ta = _FakeTaskAgent()
    result = _cmd("/new", user_id="u_stranger", chat_id="555", chat_type="dm")
    out = await act_on_inbound(ta, result, spawn=lambda c: None)
    assert ta.cancelled == ["sess_1"]
    assert ta.unbound == ["agent:main:telegram:dm:555:u_stranger"]
    assert out == "Started fresh — send your next message to begin."


@pytest.mark.asyncio
async def test_new_permitted_for_owner_in_group(env):
    ta = _FakeTaskAgent()
    result = _cmd("/new", user_id="alice", chat_id="999", chat_type="group")
    out = await act_on_inbound(ta, result, spawn=lambda c: None)
    assert ta.cancelled == ["sess_1"]
    assert out == "Started fresh — send your next message to begin."
