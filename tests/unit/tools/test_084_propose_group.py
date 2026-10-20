"""084 WS1/WS5: propose_action groups its cards; a failed confirm offers the
extra check as a card in the same group."""
import asyncio

import pytest

from core.surfaces import card_groups as G
from core.surfaces import cards
from core.wallet.tx_guard_steps.value import UNVERIFIED_MARK

U = "owner1"
LINE = ["18", "0x" + "ab" * 20, "to", "0x" + "cd" * 20, "on", "robinhood"]


@pytest.fixture
def st(tmp_path):
    store = cards.CardStore(str(tmp_path / "cards.db"))
    cards.set_store(store)
    G._GROUPS.clear()
    yield store
    cards.set_store(None)
    G._GROUPS.clear()


def test_a_chat_session_gets_one_group_and_its_then(st):
    from tools.controller.card_actions import _group_for, _joined
    a = _group_for(U, "chat1")
    assert a == _group_for(U, "chat1")
    _joined(a, "pay the PNL half")
    g = G.groups(st).get(a)
    assert (g["origin"], g["then_text"]) == (G.ORIGIN_CHAT, "pay the PNL half")
    assert _group_for(U, "") is None


def test_an_empty_group_waits_on_nothing(st):
    """A refused propose_action leaves a group with no card: never 'waiting'."""
    from tools.controller.card_actions import _group_for
    gid = _group_for(U, "chat1")
    assert G.groups(st).open_count("chat1") == 0
    cards.proposal_card(U, "/send", LINE, session_id="chat1", group_id=gid)
    assert G.groups(st).open_count("chat1") == 1


def test_a_card_added_to_a_just_settled_group_reopens_it(st):
    gid = G.groups(st).open_group_for(U, "chat1")
    p = cards.proposal_card(U, "/send", LINE, session_id="chat1", group_id=gid)
    cards.press(p.card_id, "no", U)
    assert G.groups(st).get(gid)["state"] == G.G_SETTLED
    late = cards.proposal_card(U, "/send", LINE, session_id="chat1", group_id=gid)
    G.rejoin(gid, st)
    assert G.groups(st).get(gid)["state"] == G.G_OPEN
    cards.press(late.card_id, "no", U)
    assert G.groups(st).get(gid)["state"] == G.G_SETTLED


def test_a_goal_run_names_its_goal(st, monkeypatch):
    import core.autonomy_marker as am
    am.mark_autonomous("goal-sess", "g42")
    from tools.controller.card_actions import _group_for
    g = G.groups(st).get(_group_for(U, "goal-sess"))
    assert (g["origin"], g["origin_ref"]) == (G.ORIGIN_GOAL, "g42")


def test_a_failed_unverified_confirm_offers_the_extra_check_as_a_card(st, monkeypatch):
    from surfaces.telegram import card_ops
    sent = []

    async def _deliver(container, user_id, text, **kw):
        sent.append(kw)
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)
    gid = G.groups(st).open_group_for(U, "chat1")
    q = cards._new(U, cards.KIND_QUOTE, cards.ORIGIN_SYSTEM, "/send — quote", verb="/send",
                   args=LINE + ["max", "18.9"], typed=LINE, session_id="chat1",
                   group_id=gid, ttl=cards.QUOTE_TTL_S)
    cards.press(q.card_id, "ok", U)
    q = cards.store().get(q.card_id)
    out = (f"❌ Nothing was sent. DRY RUN … {UNVERIFIED_MARK}\n⚠️ EXTRA CHECK\n"
           f"To continue: /send {' '.join(LINE)} anyway")
    asyncio.run(card_ops._recover(None, q, out))
    assert len(sent) == 1 and sent[0]["card_id"]
    ack = cards.store().get(sent[0]["card_id"])
    assert ack.args[-1] == "anyway" and ack.group_id == gid and ack.parent_id == q.card_id
    cards.finish(q.card_id, out)
    assert G.groups(st).get(gid)["state"] == G.G_OPEN
