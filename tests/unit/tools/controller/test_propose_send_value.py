"""A /send card shows the token amount AND its USD value when it is proposed
(prod 2026-10-28: cards meant as $22 carried `/send 22 <token>` = $0.05, and
nothing on the card said so). A `$` amount is converted by the tool; a reason
whose dollar figure is >10x off the command gets a warning line, never a
refusal."""
import asyncio

from core.surfaces import cards
from tests.unit.tools.controller.test_card_actions import _actions, _ctx, _owner  # noqa: F401

TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
A = "0xf3105aA1F74F5cD05E0969053A7b56684cffa258"
PX = 0.0000271


def _price(monkeypatch, px):
    monkeypatch.setattr("tools.controller.card_actions._SEND_PRICE_FN", lambda c, a: px)


def _propose(cmd, why):
    fn, model, _ = _actions()["propose_action"]
    return asyncio.run(fn(model(command=cmd, why=why), execution_context=_ctx()))


def _card():
    open_ = [c for c in cards.store().open_cards("alice", limit=10) if c.verb == "/send"]
    assert len(open_) == 1
    return open_[0]


def test_a_token_amount_card_shows_its_dollar_value_and_flags_the_mismatch(_owner, monkeypatch):  # noqa: F811
    _price(monkeypatch, PX)
    res = _propose(f"/send 22 {TOKEN} to {A} on base", "round 1: alice — $22 payout")
    assert res.error is None
    body = _card().body
    assert "22 tokens = $0.0005962" in body                 # 22 x $0.0000271
    assert "MISMATCH" in body and "$22.00" in body and "/send $22" in body
    assert "MISMATCH" in res.extracted_content             # the model sees it too


def test_a_dollar_card_shows_the_converted_token_count(_owner, monkeypatch):  # noqa: F811
    _price(monkeypatch, PX)
    res = _propose(f"/send $22 {TOKEN} to {A} on base", "round 1: alice — $22 payout")
    assert res.error is None
    card = _card()
    assert card.args[0] == "$22"                           # the tap converts again
    assert "$22.00 = 811,808 tokens" in card.body and "MISMATCH" not in card.body


def test_a_dollar_card_without_a_spend_grade_price_is_refused(_owner, monkeypatch):  # noqa: F811
    _price(monkeypatch, None)
    res = _propose(f"/send $22 {TOKEN} to {A} on base", "payout")
    assert res.error and "No spend-grade price" in res.error
    assert not [c for c in cards.store().open_cards("alice", limit=10) if c.verb == "/send"]


def test_an_unpriced_token_card_says_the_value_is_not_verified(_owner, monkeypatch):  # noqa: F811
    _price(monkeypatch, None)
    res = _propose(f"/send 22 {TOKEN} to {A} on base", "payout of $22")
    assert res.error is None
    body = _card().body
    assert "NO spend-grade price" in body
    # review L3: an unpriced micro-cap is exactly where `/send 22` meant "$22"
    assert "MISMATCH RISK" in body and "$22.00" in body and "TOKEN count" in body


def test_a_close_figure_in_the_reason_is_not_flagged(_owner, monkeypatch):  # noqa: F811
    _price(monkeypatch, 1.0)
    res = _propose(f"/send 22 {TOKEN} to {A} on base", "pool $150; alice $22")
    assert res.error is None and "MISMATCH" not in _card().body


def test_a_failed_price_read_still_makes_the_card(_owner, monkeypatch):  # noqa: F811
    def boom(c, a):
        raise TimeoutError("slow")
    monkeypatch.setattr("tools.controller.card_actions._SEND_PRICE_FN", boom)
    res = _propose(f"/send $22 {TOKEN} to {A} on base", "payout")
    assert res.error is None and "could not be read now" in _card().body


def test_the_command_doc_teaches_the_dollar_form():
    _, model, _ = _actions()["propose_action"]
    doc = model.model_fields["command"].description
    assert "/send $22" in doc and "TOKEN count" in doc
