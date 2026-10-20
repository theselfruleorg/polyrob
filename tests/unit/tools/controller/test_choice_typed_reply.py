"""A typed reply to an open present_choice card ends the wait at once.

Prod 2026-10-15 04:09: the owner typed "Judge" instead of tapping; the line was
queued as a STEER while present_choice blocked until the action timeout. The
card now closes as "replied in text" — the text is NEVER parsed into a pick —
and the agent is told to read the queued message as the owner's answer.
"""
import asyncio
import threading
import time

import pytest

from core.surfaces import cards
from tests.unit.tools.controller.test_card_actions import _actions, _ctx, _owner  # noqa: F401


def _choice(sid="s-owner"):
    return cards.choice_card("rob", "Which round?", ["Judge", "Drop"], session_id=sid, ttl=300)


def test_closes_the_open_choice_in_that_session_without_a_pick():
    c = _choice()
    closed = cards.close_choices_for_text("rob", "s-owner", "Judge")
    assert [x.card_id for x in closed] == [c.card_id]
    got = cards.store().get(c.card_id)
    assert got.state == cards.S_CANCELLED and got.answer is None
    assert got.result.startswith(cards.TEXT_REPLY_RESULT)


def test_leaves_other_sessions_and_users_alone():
    c = _choice("s-other")
    assert cards.close_choices_for_text("rob", "s-owner", "x") == []
    assert cards.close_choices_for_text("someone", "s-other", "x") == []
    assert cards.store().get(c.card_id).state == cards.S_OPEN


def test_no_session_closes_nothing():
    _choice()
    assert cards.close_choices_for_text("rob", "", "x") == []


def test_present_choice_returns_promptly_on_a_typed_reply(_owner):  # noqa: F811
    fn, model, _ = _actions()["present_choice"]
    params = model(question="Which round?", options=["Judge", "Drop"], wait_seconds=60)

    def _type_soon():
        for _ in range(100):
            if cards.store().open_cards("rob"):
                cards.close_choices_for_text("rob", "s-owner", "Judge")
                return
            time.sleep(0.05)

    t = threading.Thread(target=_type_soon)
    t.start()
    started = time.time()
    res = asyncio.run(fn(params, execution_context=_ctx()))
    t.join()
    assert time.time() - started < 10
    assert "replied in text" in res.extracted_content and "picked" not in res.extracted_content


def test_an_unreadable_store_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(cards.store(), "open_cards", _boom)
    assert cards.close_choices_for_text("rob", "s-owner", "x") == []
