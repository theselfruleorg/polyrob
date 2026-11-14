"""The /cards list and the cancel of a card by /cancel (prod 2026-11-13).

The owner typed /cancel to drop a card and it stopped a task; the list printed
"(proposal, from agent) — read it: …" with a "Dismiss" act that no command named.
"""
from core.surfaces import cards
from core.surfaces.card_list import cancel_reply, card_summary, list_reply

TOKEN = "0x" + "ab" * 20
TO = "0x" + "cd" * 20


def _store(tmp_path):
    return cards.CardStore(str(tmp_path / "c.db"))


def _proposal(st, to=TO):
    return cards.proposal_card("u1", "/send", ["177103", TOKEN, "to", to, "on", "robinhood"],
                               why="C2: @winner — $28", st=st)


def test_a_send_proposal_reads_as_the_send(tmp_path):
    st = _store(tmp_path)
    card = _proposal(st)
    assert "💸 Send 177,103 0xabab…abab → 0xcdcd…cdcd on robinhood (proposal)" in card_summary(card)


def test_the_list_is_three_short_lines_per_card_and_says_cancel(tmp_path):
    st = _store(tmp_path)
    card = _proposal(st)
    text = list_reply("u1", st=st)
    assert text.startswith("🃏 Open cards: 1")
    assert "C2: @winner — $28" in text
    assert f"Get a quote /card_{card.card_id}_re · Cancel /card_{card.card_id}_no" in text
    assert "Dismiss" not in text and "/cancel cards" in text


def test_cancel_with_a_card_id_cancels_that_card(tmp_path):
    st = _store(tmp_path)
    card = _proposal(st)
    assert cancel_reply("u1", [card.card_id], st=st).startswith("✖️ Cancelled")
    assert st.get(card.card_id).state == cards.S_CANCELLED


def test_cancel_cards_cancels_every_open_card(tmp_path):
    st = _store(tmp_path)
    a, b = _proposal(st), _proposal(st, to="0x" + "12" * 20)
    out = cancel_reply("u1", ["cards"], st=st)
    assert "Cancelled 2 card(s)" in out
    assert {st.get(a.card_id).state, st.get(b.card_id).state} == {cards.S_CANCELLED}
    assert cancel_reply("u1", ["cards"], st=st) == "No open cards. Nothing was cancelled."


def test_other_words_leave_cancel_to_the_task(tmp_path):
    st = _store(tmp_path)
    assert cancel_reply("u1", [], st=st) is None
    assert cancel_reply("u1", ["please"], st=st) is None


def test_another_owner_cannot_cancel_my_card(tmp_path):
    st = _store(tmp_path)
    card = _proposal(st)
    cancel_reply("u2", [card.card_id], st=st)
    assert st.get(card.card_id).state == cards.S_OPEN


def test_the_typed_act_words_match_the_buttons():
    assert cards.parse_act("cancel") == "no" and cards.parse_act("Confirm") == "ok"
    assert cards.parse_act("quote") == "re" and cards.parse_act("2") == "2"
    assert cards.parse_act("dismiss") is None


def test_the_agent_reads_open_and_decided_cards_with_their_counts(tmp_path):
    from core.surfaces.card_list import agent_view
    st = _store(tmp_path)
    old = _proposal(st)
    st.transition(old.card_id, frozenset({cards.S_OPEN}), cards.S_REPLACED)
    new = cards.proposal_card("u1", "/send", ["120177", TOKEN, "to", TO, "on", "robinhood"],
                              why="C2: @winner — $19", st=st)
    text = agent_view("u1", st=st)
    open_part, decided_part = text.split("DECIDED")
    assert new.card_id in open_part and "120177" in open_part
    assert old.card_id in decided_part and "replaced" in decided_part
    assert old.card_id not in open_part


def test_an_unreadable_store_is_not_no_cards():
    from core.surfaces.card_list import agent_view

    class Broken:
        def open_cards(self, *a, **k):
            raise OSError("disk")
    assert agent_view("u1", st=Broken()).startswith("UNREADABLE")
