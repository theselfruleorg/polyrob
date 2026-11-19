"""120 W2: cards_read(cancel=<id>) withdraws an OPEN card the agent raised, through the
store's own compare-and-set. A card the system raised for the owner, or a card that is
not open, is refused; a decision the owner made first wins."""
import asyncio

import pytest

from core.surfaces import cards


class _Registry:
    def __init__(self):
        self.actions = {}

    def action(self, description, param_model=None, **kw):
        def deco(fn):
            self.actions[fn.__name__] = (fn, param_model)
            return fn
        return deco


class _Controller:
    def __init__(self):
        self.registry = _Registry()
        self.container = type("C", (), {"get_service": staticmethod(lambda name: None)})()
        self.user_id = "alice"
        self.session_id = "s-owner"
        self.orchestrator = type("O", (), {"_public_session": False,
                                           "_forged_turn_kind": None})()


def _ctx():
    return type("Ctx", (), {"user_id": "alice", "role": "orchestrator", "is_sub_agent": False,
                            "session_id": "s-owner", "metadata": {}})()


@pytest.fixture(autouse=True)
def _store(monkeypatch, tmp_path):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "alice")
    cards.set_store(cards.CardStore(str(tmp_path / "cards.db")))
    yield
    cards.set_store(None)


def _cards_read():
    from tools.controller.card_actions import register_card_actions
    c = _Controller()
    register_card_actions(c)
    fn, model = c.registry.actions["cards_read"]
    return lambda **kw: asyncio.run(fn(model(**kw), execution_context=_ctx()))


def _agent_card():
    return cards._new("alice", cards.KIND_PROPOSAL, cards.ORIGIN_AGENT, "Send 1 USDC",
                      verb="/send", args=["1", "usdc"], ttl=3600)


def test_the_agent_withdraws_its_own_open_card():
    card = _agent_card()
    res = _cards_read()(cancel=card.card_id)
    assert not res.error and f"Cancelled card {card.card_id}" in res.extracted_content
    after = cards.store().get(card.card_id)
    assert after.state == cards.S_CANCELLED and after.result == "withdrawn by the agent"


def test_a_card_the_system_raised_for_the_owner_is_refused():
    card = cards._new("alice", cards.KIND_QUOTE, cards.ORIGIN_SYSTEM, "Quote", verb="/send",
                      args=["1", "usdc"], ttl=600)
    res = _cards_read()(cancel=card.card_id)
    assert res.error and "only the owner cancels it" in res.error
    assert cards.store().get(card.card_id).state == cards.S_OPEN


def test_a_card_that_is_not_open_is_refused():
    card = _agent_card()
    cards.store().transition(card.card_id, frozenset({cards.S_OPEN}), cards.S_CONFIRMED)
    res = _cards_read()(cancel=card.card_id)
    assert res.error and "not open" in res.error
    assert cards.store().get(card.card_id).state == cards.S_CONFIRMED


def test_a_confirm_in_flight_wins_the_compare_and_set(monkeypatch):
    card = _agent_card()
    real = cards.CardStore.transition

    def _owner_first(self, card_id, expect, new_state, **kw):
        real(self, card_id, frozenset({cards.S_OPEN}), cards.S_CONFIRMED)
        return real(self, card_id, expect, new_state, **kw)

    monkeypatch.setattr(cards.CardStore, "transition", _owner_first)
    res = _cards_read()(cancel=card.card_id)
    assert res.error and "changed first" in res.error
    assert cards.store().get(card.card_id).state == cards.S_CONFIRMED


def test_an_unknown_card_is_named():
    res = _cards_read()(cancel="zz99")
    assert res.error and "No card zz99" in res.error


def test_without_cancel_nothing_changes():
    card = _agent_card()
    res = _cards_read()()
    assert not res.error and card.card_id in res.extracted_content
    assert cards.store().get(card.card_id).state == cards.S_OPEN
