"""086 W5: ``trade_mandate_propose`` — the agent's intake ends in ONE owner card.

The card's numbers come from the validated draft + the preset, never from model
text; nothing is created before the owner's Confirm; the Confirm line is the
owner seat's own ``/trade start <ref>``."""
import asyncio

import pytest

from core import trading_mandate as tm
from core.surfaces import cards


class _Registry:
    def __init__(self):
        self.actions = {}

    def action(self, description, param_model=None, **kw):
        def deco(fn):
            self.actions[fn.__name__] = (fn, param_model, description)
            return fn
        return deco


class _Controller:
    def __init__(self, public=False):
        self.registry = _Registry()
        self.container = None
        self.user_id = "rob"
        self.session_id = "s-owner"
        self.orchestrator = type("O", (), {"_public_session": public,
                                           "_forged_turn_kind": None})()


def _ctx(role="orchestrator"):
    return type("Ctx", (), {"user_id": "rob", "role": role, "is_sub_agent": False,
                            "session_id": "s-owner", "metadata": {}})()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")
    monkeypatch.setattr(tm, "db_path", lambda data_dir=None: str(tmp_path / "trading.db"))
    cards.set_store(cards.CardStore(str(tmp_path / "cards.db")))
    sent = []

    async def _deliver(container, user_id, text, **kw):
        sent.append((user_id, text, kw))
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)
    yield sent
    cards.set_store(None)


def _act(public=False):
    from tools.controller.trade_mandate_action import register_trade_mandate_action
    c = _Controller(public=public)
    register_trade_mandate_action(c)
    return c.registry.actions["trade_mandate_propose"]


def _run(public=False, ctx=None, **params):
    fn, model, _ = _act(public)
    return asyncio.run(fn(model(**params), execution_context=ctx or _ctx()))


def test_posts_one_card_whose_line_is_the_seat_start(_env):
    res = _run(budget_usd=150, chains=["robinhood", "solana"], preset="memecoin")
    assert res.error is None and "NOTHING is started" in res.extracted_content
    (card,) = cards.store().open_cards("rob")
    assert card.kind == cards.KIND_JOB and card.origin == cards.ORIGIN_AGENT
    assert card.line.startswith("/trade start d") and len(card.args) == 2
    assert "$150 allocation" in card.body and "$3 tickets" in card.body
    assert "-50% stop" in card.body                     # the preset's number
    (_u, text, kw), = _env
    assert kw["card_id"] == card.card_id and "Confirm:" in text
    assert tm.get_active("rob") is None                 # nothing before Confirm


def test_a_bad_value_makes_no_card():
    res = _run(budget_usd=5, chains=["base"], ticket_usd=50)
    assert res.error and "no card" in res.error
    assert cards.store().open_cards("rob") == []


def test_one_live_mandate_at_a_time():
    tm.create("rob", actor=tm.OWNER_SEAT, budget_usd=10, chains=["base"])
    res = _run(budget_usd=150, chains=["solana"])
    assert res.error and "already" in res.error


@pytest.mark.parametrize("public,role,needle", [(True, "orchestrator", "room"),
                                                (False, "leaf", "leaf")])
def test_refused_in_a_room_and_for_a_leaf(public, role, needle):
    res = _run(public=public, ctx=_ctx(role=role), budget_usd=150, chains=["solana"])
    assert res.error and needle in res.error


def test_never_handed_to_a_sub_agent():
    from tools.controller.delegation import DELEGATION_ACTION_NAMES
    assert "trade_mandate_propose" in DELEGATION_ACTION_NAMES
