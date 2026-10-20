"""A second /send proposal to an address that already has an OPEN /send card
must not silently stack (prod 2026-10-15 07:17: 10 open cards, 4 stale
$33.33 ones to the same winners — ~$233 for a $100 pool). The agent either
names the cards it replaces, or says both payments are owed."""
import asyncio

from core.surfaces import cards
from tests.unit.tools.controller.test_card_actions import _actions, _ctx, _owner  # noqa: F401

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
A = "0xf3105aA1F74F5cD05E0969053A7b56684cffa258"
B = "0xa6a57d9867194744abd0000000000000000000aa"


def _propose(fn, model, cmd, **kw):
    return asyncio.run(fn(model(command=cmd, why="bounty", **kw), execution_context=_ctx()))


def _open_sends():
    return [c for c in cards.store().open_cards("rob", limit=100) if c.verb == "/send"]


def test_second_send_to_same_address_is_refused_and_names_the_open_card(_owner):  # noqa: F811
    fn, model, _ = _actions()["propose_action"]
    first = _propose(fn, model, f"/send 33.33 {USDC} to {A} on base")
    assert first.error is None
    old_id = _open_sends()[0].card_id
    res = _propose(fn, model, f"/send 17 {USDC} to {A.lower()} on base")
    assert res.error and old_id in res.error
    assert [c.card_id for c in _open_sends()] == [old_id]


def test_replaces_closes_the_old_card(_owner):  # noqa: F811
    fn, model, _ = _actions()["propose_action"]
    _propose(fn, model, f"/send 33.33 {USDC} to {A} on base")
    old_id = _open_sends()[0].card_id
    res = _propose(fn, model, f"/send 17 {USDC} to {A} on base", replaces=[old_id])
    assert res.error is None and old_id in res.extracted_content
    assert cards.store().get(old_id).state == cards.S_REPLACED
    assert [c.args[0] for c in _open_sends()] == ["17"]


def test_also_owed_keeps_both(_owner):  # noqa: F811
    fn, model, _ = _actions()["propose_action"]
    _propose(fn, model, f"/send 30 {USDC} to {A} on base")
    res = _propose(fn, model, f"/send 17 {USDC} to {A} on base", also_owed=True)
    assert res.error is None and len(_open_sends()) == 2


def test_different_address_is_unaffected(_owner):  # noqa: F811
    fn, model, _ = _actions()["propose_action"]
    _propose(fn, model, f"/send 30 {USDC} to {A} on base")
    assert _propose(fn, model, f"/send 22 {USDC} to {B} on base").error is None
    assert len(_open_sends()) == 2


def test_replaces_never_closes_a_non_proposal_or_another_users_card(_owner):  # noqa: F811
    # An id in `replaces` is not authority: an owner's choice card and another
    # tenant's proposal stay open (release review 2026-10-18).
    fn, model, _ = _actions()["propose_action"]
    choice = cards.choice_card("rob", "Which one?", ["a", "b"])
    other = cards.proposal_card("someone-else", "/send",
                                ["5", USDC, "to", B, "on", "base"])
    res = _propose(fn, model, f"/send 17 {USDC} to {A} on base",
                   replaces=[choice.card_id, other.card_id])
    assert res.error is None
    assert cards.store().get(choice.card_id).state == cards.S_OPEN
    assert cards.store().get(other.card_id).state == cards.S_OPEN
    assert "Replaced" not in res.extracted_content
