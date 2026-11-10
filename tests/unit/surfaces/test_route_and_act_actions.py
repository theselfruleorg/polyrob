"""F-09 (113 W7): a carded command reply shows its buttons on a surface that
renders them.

``route_and_act`` used to deliver text only, so on every thin-client surface the
owner had to type ``/card_<id>_ok``. Now a ``CommandReply`` with a ``card_id``
goes through the registered ``Surface.send`` with the card's actions when the
surface ``supports_actions``; anywhere else (or on a failed send) the text goes
out as before, and it keeps every command.
"""
import pytest

from core.surfaces import cards
from core.surfaces.command_reply import CommandReply
from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import (Identity, InboundMessage, SendResult, SessionSource,
                                     SurfaceCapabilities)
from core.surfaces.registry import SurfaceRegistry


class _Surface:
    surface_id = "feishu"

    def __init__(self, actions=True, ok=True):
        self.capabilities = SurfaceCapabilities(supports_actions=actions)
        self.sent, self.ok = [], ok

    async def send(self, msg):
        self.sent.append(msg)
        return SendResult(success=self.ok)


class _Container:
    def __init__(self, surface):
        self.reg = SurfaceRegistry()
        self.reg.add(surface)

        self.router = _Router(surface)

    def get_service(self, name):
        return {"surface_registry": self.reg, "message_router": self.router}.get(name)


class _Router:
    """The one outbound seam: the buttons go through ``publish``, never a direct send."""

    def __init__(self, surface):
        self.surface = surface

    async def publish(self, msg):
        return bool((await self.surface.send(msg)).success)


@pytest.fixture
def card(tmp_path):
    st = cards.CardStore(str(tmp_path / "cards.db"))
    cards.set_store(st)
    yield cards.choice_card("owner", "Which one?", ["red", "blue"], st=st)
    cards.set_store(None)


def _wire(monkeypatch, reply):
    async def _route(container, inbound):
        return RouteDecision(RouteKind.COMMAND, "agent:main:feishu:dm:c1:owner",
                             command="/cards")

    async def _act(task_agent, result, **kw):
        return reply
    monkeypatch.setattr("core.surfaces.dispatcher.route_inbound", _route)
    monkeypatch.setattr("surfaces._actor.act_on_inbound", _act)


def _inbound():
    return InboundMessage(text="/cards", identity=Identity(
        user_id="owner", source=SessionSource("feishu", "c1", "dm"), raw_user_id="c1"))


async def _run(container):
    from surfaces._shared import route_and_act
    sent = []

    async def deliver(text):
        sent.append(text)
    await route_and_act(container, None, _inbound(), deliver)
    return sent


@pytest.mark.asyncio
async def test_card_reply_goes_out_with_its_buttons(monkeypatch, card):
    _wire(monkeypatch, CommandReply(cards.render_text(card), card_id=card.card_id))
    surface = _Surface()
    assert await _run(_Container(surface)) == []
    assert len(surface.sent) == 1
    msg = surface.sent[0]
    assert [a.command for a in msg.actions] == [a.command for a in cards.card_actions(card)]
    assert msg.session_key == "agent:main:feishu:dm:c1:owner"
    assert msg.text == cards.render_text(card)


@pytest.mark.asyncio
async def test_a_surface_without_buttons_gets_the_text(monkeypatch, card):
    _wire(monkeypatch, CommandReply("card text", card_id=card.card_id))
    surface = _Surface(actions=False)
    assert await _run(_Container(surface)) == ["card text"]
    assert surface.sent == []


@pytest.mark.asyncio
async def test_a_failed_buttons_send_falls_back_to_the_text(monkeypatch, card):
    _wire(monkeypatch, CommandReply("card text", card_id=card.card_id))
    assert await _run(_Container(_Surface(ok=False))) == ["card text"]


@pytest.mark.asyncio
async def test_a_plain_reply_is_unchanged(monkeypatch, card):
    _wire(monkeypatch, "just text")
    surface = _Surface()
    assert await _run(_Container(surface)) == ["just text"]
    assert surface.sent == []


@pytest.mark.asyncio
async def test_an_unknown_card_sends_the_text(monkeypatch, card):
    _wire(monkeypatch, CommandReply("card text", card_id="nope"))
    surface = _Surface()
    assert await _run(_Container(surface)) == ["card text"]
    assert surface.sent == []
