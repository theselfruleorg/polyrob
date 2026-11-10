"""113 W2: the five chat verbs on a chat surface, the focused-chat route and the
label on a line from a chat the person is not in."""
import pytest

from core.surfaces.chats import reply_prefix
from core.surfaces.dispatcher import RouteDecision, RouteKind, route_inbound
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.session_chat_registry import SessionChatRegistry
from surfaces.telegram.harness import _UNAUTHORIZED_TEXT, act_on_inbound
from surfaces.telegram.inbound import InboundResult

BASE = "agent:main:telegram:dm:555:u_abc"


class _Container:
    def __init__(self, registry):
        self._svc = {"session_chat_registry": registry}

    def get_service(self, name):
        return self._svc.get(name)


class _TaskAgent:
    def __init__(self, registry):
        self.container = _Container(registry)
        self.cancelled, self.unbound, self.episodes = [], [], []

    async def cancel_session_by_id(self, session_id, force=False):
        self.cancelled.append(session_id)
        return True

    def unbind_chat(self, session_key):
        self.unbound.append(session_key)

    def close_chat_episode(self, session_key):
        self.episodes.append(session_key)


@pytest.fixture
def reg(tmp_path):
    registry = SessionChatRegistry(str(tmp_path / "surfaces.db"))
    registry.bind(BASE, "sess_main", "u_abc", "telegram", "555")
    return registry


def _inbound(text, *, user="u_abc", chat="555", chat_type="dm"):
    src = SessionSource("telegram", chat, chat_type)
    return InboundMessage(text=text, identity=Identity(user_id=user, source=src, raw_user_id=chat))


async def _say(ta, text, **kw):
    """Route the line as the dispatcher does, then act on it."""
    inbound = _inbound(text, **kw)
    decision = await route_inbound(ta.container, inbound)
    out = await act_on_inbound(ta, InboundResult(inbound=inbound, decision=decision),
                               spawn=lambda c: None)
    return decision, out


@pytest.mark.asyncio
async def test_new_opens_a_chat_and_the_next_line_starts_it(reg):
    ta = _TaskAgent(reg)
    decision, out = await _say(ta, "/new tax letter")
    assert decision.kind == RouteKind.COMMAND and "New chat: tax letter" in out
    assert ta.cancelled == [] and ta.unbound == []      # main keeps its session
    assert reg.resolve(BASE)["session_id"] == "sess_main"
    nxt = await route_inbound(ta.container, _inbound("draft it"))
    assert nxt.kind == RouteKind.TASK_AGENT              # cold: the first line starts it
    assert nxt.session_key == BASE + ":chat:c2"


@pytest.mark.asyncio
async def test_a_line_goes_to_the_focused_chat_and_main_stays_warm(reg):
    ta = _TaskAgent(reg)
    await _say(ta, "/new side")
    reg.bind(BASE + ":chat:c2", "sess_side", "u_abc", "telegram", "555")
    steer = await route_inbound(ta.container, _inbound("more"))
    assert (steer.kind, steer.session_id) == (RouteKind.STEER, "sess_side")
    _, out = await _say(ta, "/chat main")
    assert "Now in chat: main" in out
    back = await route_inbound(ta.container, _inbound("hello"))
    assert (back.kind, back.session_id, back.session_key) == (RouteKind.STEER, "sess_main", BASE)


@pytest.mark.asyncio
async def test_the_list_has_a_tap_and_the_tap_switches(reg):
    ta = _TaskAgent(reg)
    await _say(ta, "/new side")
    _, listing = await _say(ta, "/chats")
    assert "1. main" in listing and "/chat_main" in listing and "you are here" in listing
    decision, out = await _say(ta, "/chat_main")
    assert decision.kind == RouteKind.COMMAND and "Now in chat: main" in out
    _, out = await _say(ta, "/chat_c2")
    assert "Now in chat: side" in out


@pytest.mark.asyncio
async def test_close_writes_the_episode_and_returns_to_main(reg):
    ta = _TaskAgent(reg)
    await _say(ta, "/new side")
    _, out = await _say(ta, "/close")
    assert "Closed: side" in out and ta.episodes == [BASE + ":chat:c2"]
    nxt = await route_inbound(ta.container, _inbound("hi"))
    assert nxt.session_key == BASE
    _, out = await _say(ta, "/close")
    assert "stays open" in out and ta.episodes == [BASE + ":chat:c2"]


@pytest.mark.asyncio
async def test_rename_names_the_chat_in_the_list(reg):
    ta = _TaskAgent(reg)
    _, out = await _say(ta, "/rename home")
    assert "home" in out
    _, listing = await _say(ta, "/chats")
    assert "main (home)" in listing


@pytest.mark.asyncio
async def test_a_sender_cannot_run_a_chat_verb_on_a_key_that_is_not_theirs(reg):
    ta = _TaskAgent(reg)
    inbound = _inbound("/new x", user="u_other", chat="777")
    result = InboundResult(inbound=inbound,
                           decision=RouteDecision(RouteKind.COMMAND, BASE, command="/new"))
    assert await act_on_inbound(ta, result, spawn=lambda c: None) == _UNAUTHORIZED_TEXT
    assert [r["session_key"] for r in reg.chat_rows_for_base(BASE)] == [BASE]


def test_a_line_from_the_chat_you_left_names_that_chat(reg):
    from core.surfaces import chats
    chats.chat_verb(reg, "/new", ["side"], base_key=BASE, user_id="u_abc",
                    surface_id="telegram", chat_id="555")
    assert reply_prefix(reg, BASE) == "[main] "
    assert reply_prefix(reg, BASE + ":chat:c2") == ""
    assert reply_prefix(reg, "agent:main:telegram:group:-9") == ""
    assert reply_prefix(None, BASE) == ""


class _StartingAgent(_TaskAgent):
    async def create_session(self, user_id, request=None, **kwargs):
        self.created = {"user_id": user_id, "request": request, **kwargs}
        return {"id": "sess_new"}

    async def run_session(self, user_id, session_id):
        return "ok"

    def get_orchestrator(self, session_id):
        return None

    def touch_chat_binding(self, session_key):
        pass

    def _extract_chat_reply(self, session_id, since=None):
        return ""


@pytest.mark.asyncio
async def test_task_is_new_plus_the_first_line_and_the_bound_chat_keeps_its_run(reg):
    """113 D6: two /task lines used to leave the first run with no chat."""
    ta, told = _StartingAgent(reg), []

    async def deliver(text, **kw):
        told.append(text)

    inbound = _inbound("/task write the tax letter")
    decision = await route_inbound(ta.container, inbound)
    ran = []
    out = await act_on_inbound(ta, InboundResult(inbound=inbound, decision=decision),
                               spawn=lambda c: ran.append(c) or c.close(), deliver=deliver)
    assert out is None and ta.cancelled == [] and ta.unbound == []
    assert ta.created["chat_session_key"] == BASE + ":chat:c2"
    assert "write the tax letter" in str(ta.created["request"])
    assert any("/task is now /new" in line for line in told)
    assert reg.resolve(BASE)["session_id"] == "sess_main"        # main keeps its session


@pytest.mark.asyncio
async def test_task_with_no_goal_starts_nothing(reg):
    ta = _StartingAgent(reg)
    _, out = await _say(ta, "/task")
    assert "/new" in out and not hasattr(ta, "created")
    assert len(reg.chat_rows_for_base(BASE)) == 1
