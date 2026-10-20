"""079 W5 (R5/R6): a guard refusal is never a confirmable card and never ends
with "add `go`"; owner-verb outcomes and card taps reach the owner thread as
the words they stand for, not as an opaque token."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from core.surfaces import cards
from core.surfaces import owner_thread as ot
from core.surfaces.command_reply import CommandReply, reply_text
from tests.unit.surfaces.telegram.test_card_flow import ADDR, _agent, _run, env  # noqa: F401

_REFUSED_LANE = ("bridge robinhood -> base\n  guard: refused by PolicyGate: amount $329.00 "
                 "exceeds the per-transaction cap $200.00\n  lane:  refuse\n")
_NOT_SENT = "transfer\n  guard: no\n  RESULT: NOT SENT — nothing was broadcast."
_GOOD = ("bridge robinhood -> base\n  phase 1: assertions passed\n"
         "  lane:   autonomous — within the $300.00 ceiling\n[DRY RUN] nothing was broadcast.\n\n"
         "Add `go` to execute: /bridge robinhood base 0.13 go")


@pytest.fixture
def store(tmp_path):
    st = cards.CardStore(str(tmp_path / "cards.db"))
    return st


# --- the refusal check -----------------------------------------------------

@pytest.mark.parametrize("text", [
    _REFUSED_LANE, _NOT_SENT,
    "lp add\nguard: refused; value: 9.0; lane: refuse\n",
])
def test_a_structured_refusal_is_recognized(text):
    assert cards.refused_quote(text)


def test_a_coarse_failure_word_is_not_a_refusal():
    # `failed_result` matches any "refused" — a value-check note in a good quote.
    text = "  independent value check: a cheaper route was refused upstream\n" + _GOOD
    assert cards.failed_result(text)
    assert not cards.refused_quote(text)
    assert not cards.refused_quote("lane:  owner_queue")


@pytest.mark.parametrize("body", [_REFUSED_LANE + "  RESULT: NOT SENT.", _NOT_SENT])
def test_a_refused_quote_makes_no_card(store, body):
    reply = body + "\n\nAdd `go` to execute: /bridge robinhood base 0.13 go"
    text, card = cards.quote_card("alice", "/bridge", ["robinhood", "base", "0.13"],
                                  reply, st=store)
    assert card is None and text == reply


def test_a_good_quote_is_still_a_card(store):
    text, card = cards.quote_card("alice", "/bridge", ["robinhood", "base", "0.13"],
                                  _GOOD, st=store)
    assert card is not None and card.confirm_line == "/bridge robinhood base 0.13 go"


@pytest.mark.parametrize("verb", ["/restart", "/update"])
def test_restart_and_update_stay_exempt(store, verb):
    reply = f"Pending work: one goal (lane: refuse was its last guard line).\nTo do it: {verb} go"
    _, card = cards.quote_card("alice", verb, [], reply, st=store)
    assert card is not None


def test_go_hint_only_on_a_quote_that_can_run():
    hint = "\n\nAdd `go` to execute: /x go"
    assert cards.with_go_hint(_NOT_SENT, hint) == _NOT_SENT
    assert cards.with_go_hint("quote ok", hint) == "quote ok" + hint


# --- the money seats drop the go hint on a refusal ---------------------------

def _bridge(monkeypatch, body):
    import tools.defi.bridge_verb as bv
    import tools.defi.trade_tool as tt

    async def _perform(tool, params, ctx):
        return SimpleNamespace(error=None, extracted_content=body)

    monkeypatch.setattr(bv, "perform_bridge", _perform)
    monkeypatch.setattr(tt, "DefiTradeTool", lambda: object())
    from surfaces.telegram.owner_ops import bridge_reply
    return asyncio.run(bridge_reply("alice", "", ["robinhood", "base", "0.13"]))


def test_bridge_refusal_has_no_go_hint(monkeypatch):
    out = _bridge(monkeypatch, _REFUSED_LANE + "  RESULT: NOT SENT.")
    assert "Add `go`" not in out and "per-transaction cap" in out


def test_bridge_good_quote_keeps_its_go_hint(monkeypatch):
    out = _bridge(monkeypatch, "bridge ok\n[DRY RUN] nothing was broadcast.")
    assert out.endswith("/bridge robinhood base 0.13 go")


def test_token_and_claim_seats_share_the_rule():
    from surfaces.telegram.token_ops import _render
    refused = SimpleNamespace(error=None, extracted_content=_NOT_SENT)
    good = SimpleNamespace(error=None, extracted_content="deploy quote")
    assert "Add `go`" not in _render(refused, retry_hint="/deploy X 1 go")
    assert _render(good, retry_hint="/deploy X 1 go").endswith("/deploy X 1 go")


# --- owner-verb outcomes and taps in the owner thread ------------------------

def _rows(agent):
    return ot.thread_recent(agent.container, "alice", limit=20) or []


def test_a_verb_reply_is_an_out_row(env):
    agent = _agent(env.dir)
    reply = _run(agent, f"/send 1 native to {ADDR} on ethereum")
    assert isinstance(reply, CommandReply)
    outs = [r for r in _rows(agent) if r["direction"] == "out"]
    assert outs and outs[-1]["source"] == "owner_verb"
    assert "simulated value" in outs[-1]["body"]


def test_a_card_tap_is_recorded_as_its_stored_line(env, caplog):
    agent = _agent(env.dir)
    reply = _run(agent, f"/send 1 native to {ADDR} on ethereum")
    card = cards.store().get(reply.card_id)
    with caplog.at_level(logging.INFO, logger="surfaces.telegram.card_ops"):
        out = _run(agent, f"/card_{card.card_id}_ok")
    assert "SENT" in reply_text(out)
    rows = _rows(agent)
    ins = [r["body"] for r in rows if r["direction"] == "in"]
    assert card.confirm_line in ins
    assert not any(b.startswith("/card_") for b in ins)
    assert any(r["direction"] == "out" and "SENT AND CONFIRMED" in r["body"]
               and r["source"] == "owner_verb" for r in rows)
    msgs = [r.getMessage() for r in caplog.records]
    assert any(f"card {card.card_id}: press act=ok" in m for m in msgs)
    assert any(f"card {card.card_id}: finish verb=/send state=done" in m for m in msgs)


def test_a_cancel_tap_is_recorded_in_words(env):
    agent = _agent(env.dir)
    reply = _run(agent, f"/send 1 native to {ADDR} on ethereum")
    _run(agent, f"/card_{reply.card_id}_no")
    ins = [r["body"] for r in _rows(agent) if r["direction"] == "in"]
    assert any(b.startswith("(cancelled the card:") for b in ins)


def test_tap_line_leaves_an_unknown_card_alone(store):
    assert cards.tap_line("/card_0123456789_ok", "alice", st=store) == "/card_0123456789_ok"
