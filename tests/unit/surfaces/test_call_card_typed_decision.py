"""A typed `/approve <call card id>` is the card's tap, on every seat (126).

It ran the card's transactions inline: on Telegram the poll loop read no update
(not even `/cancel`) until the receipts landed, and the console held the request.
The REPL's `/reject <card id>` did not reach a call card at all, and a bare
`/approve` on Telegram said "Nothing waits on you" with a money call open.
"""
import asyncio

import pytest

from core.surfaces import call_cards, cards
from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.harness import act_on_inbound
from surfaces.telegram.inbound import InboundResult

STEPS = [{"do": "defi_trade_transfer", "args": {"chain": "base", "amount": 1.0}}]


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


def _cmd(command, text, user="alice"):
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(text=text,
                             identity=Identity(user_id=user, source=src, raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:dm:555:" + user, command=command))


@pytest.fixture
def env(tmp_path, monkeypatch):
    from core.instance import DEFAULT_INSTANCE_ID
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", DEFAULT_INSTANCE_ID)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setattr(cards, "_STORE", cards.CardStore(str(tmp_path / "cards.db")))
    ran = []

    async def _run_confirmed(card, *, seat, container=None):
        ran.append((card.card_id, seat))
        cards.finish(card.card_id, "sent once")
        return "sent once"
    monkeypatch.setattr("tools.controller.call_card_run.run_confirmed", _run_confirmed)
    return tmp_path, ran


def _card(user="alice"):
    return call_cards.make(user, STEPS, title="Send 1 on base")


@pytest.mark.asyncio
async def test_a_typed_approve_runs_the_card_off_the_loop_and_delivers(env):
    home, ran = env
    card = _card()
    got = []

    async def deliver(text):
        got.append(text)
    out = await act_on_inbound(_Agent(str(home)), _cmd("/approve", f"/approve {card.card_id}"),
                               deliver=deliver)
    assert "Started" in out and card.card_id in out     # answered at once
    for _ in range(50):
        if got:
            break
        await asyncio.sleep(0.01)
    assert ran == [(card.card_id, "telegram")] and "sent once" in got[0]
    assert cards.store().get(card.card_id).state != cards.S_OPEN


@pytest.mark.asyncio
async def test_a_typed_reject_cancels_the_card_and_runs_nothing(env):
    home, ran = env
    card = _card()
    out = await act_on_inbound(_Agent(str(home)), _cmd("/reject", f"/reject {card.card_id}"))
    assert not ran and cards.store().get(card.card_id).state == cards.S_CANCELLED
    assert out


@pytest.mark.asyncio
async def test_another_owners_card_id_is_not_a_tap(env):
    home, ran = env
    card = _card(user="bob")
    await act_on_inbound(_Agent(str(home)), _cmd("/approve", f"/approve {card.card_id}"))
    assert not ran and cards.store().get(card.card_id).state == cards.S_OPEN


@pytest.mark.asyncio
async def test_a_bare_approve_lists_the_open_call_card_first(env):
    home, _ran = env
    card = _card()
    for verb in ("/approve", "/reject"):
        out = await act_on_inbound(_Agent(str(home)), _cmd(verb, verb))
        assert "Nothing waits on you" not in out
        assert f"/card_{card.card_id}_ok" in out


def test_the_repl_reject_with_a_card_id_cancels_the_card(env, monkeypatch):
    _home, ran = env
    card = _card()
    monkeypatch.setattr("cli._admin_home.admin_owner_tenant", lambda uid=None: "alice")
    from cli.ui.commands import h_reject

    class Ctx:
        user_id, args, container, registry = "alice", [card.card_id], None, object()

        def __init__(self):
            self.out = []

        def emit(self, text, **kw):
            self.out.append(str(text))
    ctx = Ctx()
    asyncio.run(h_reject.h_reject(ctx))
    assert not ran and cards.store().get(card.card_id).state == cards.S_CANCELLED
    assert ctx.out


def test_the_repl_bare_reject_only_lists(env, monkeypatch):
    _home, _ran = env
    card = _card()
    monkeypatch.setattr("cli._admin_home.admin_owner_tenant", lambda uid=None: "alice")
    monkeypatch.setattr("cli.ui.commands.h_reject._tenant", lambda ctx: "alice")
    from cli.ui.commands import h_reject

    class Ctx:
        user_id, args, container = "alice", [], None

        def __init__(self):
            self.out = []

        def emit(self, text, **kw):
            self.out.append(str(text))
    ctx = Ctx()
    assert h_reject.h_reject(ctx) is None
    assert f"/card_{card.card_id}_no" in ctx.out[0]
    assert cards.store().get(card.card_id).state == cards.S_OPEN
