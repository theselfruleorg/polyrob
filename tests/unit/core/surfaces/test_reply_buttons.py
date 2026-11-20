"""One button rule for every seat (core.surfaces.command_reply.reply_buttons):
a carded reply shows its card's buttons, else the reply's next-step actions,
else a decision list's tokens. Telegram, Slack/Discord and the console all
draw this list, so no seat keeps its own copy of the rule."""
import pytest

from core.surfaces import cards
from core.surfaces.command_reply import CommandReply, reply_buttons
from core.surfaces.envelopes import Action

ADDR = "0x" + "ab" * 20
SEND_QUOTE = ("Send 1 ETH to " + ADDR + " on ethereum?\n"
              "To confirm: /send 1 native to " + ADDR + " on ethereum go")


@pytest.fixture()
def st(tmp_path, monkeypatch):
    store = cards.CardStore(str(tmp_path / "c.db"))
    monkeypatch.setattr(cards, "_STORE", store)
    return store


def test_a_carded_reply_shows_its_cards_buttons(st):
    typed = ["1", "native", "to", ADDR, "on", "ethereum"]
    card = cards.quote_card("u1", "/send", typed, SEND_QUOTE, st=st)[1]
    assert card is not None
    out = reply_buttons(CommandReply("quote", card_id=card.card_id))
    assert [a.command.rsplit("_", 1)[1] for a in out] == ["ok", "re", "no"]


def test_a_decided_card_shows_none_and_falls_back_to_explicit_actions(st):
    typed = ["1", "native", "to", ADDR, "on", "ethereum"]
    card = cards.quote_card("u1", "/send", typed, SEND_QUOTE, st=st)[1]
    cards.press(card.card_id, "no", "u1", st=st)
    nav = Action("Status", "/status")
    assert reply_buttons(CommandReply("x", card_id=card.card_id)) == []
    assert reply_buttons(CommandReply("x", card_id=card.card_id, actions=(nav,))) == [nav]


def test_explicit_actions_are_rechecked(st):
    nav = Action("Status", "/status")
    assert reply_buttons(CommandReply("x", actions=(nav,))) == [nav]


def test_a_decision_list_gets_its_tokens_only_for_listing_verbs():
    text = "1. Send 1 ETH  /approve_p_a1b2c3"
    assert [a.command for a in reply_buttons(text, "/pending", "/pending")] == ["/approve_p_a1b2c3"]
    assert reply_buttons(text, "/thread", "/thread") == []   # quoted words are never buttons
    assert reply_buttons(text, "/approve", "/approve p_a1b2c3") == []


def test_never_raises_on_a_plain_string_or_none():
    assert reply_buttons("hello") == []
    assert reply_buttons(None) == []
