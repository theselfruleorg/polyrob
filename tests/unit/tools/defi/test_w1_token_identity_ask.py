"""W1: an identity refusal with no trusted candidate becomes ONE owner tap.

Prod 2026-09-25/26: the gate refused the real PNL every cycle and told the agent
to "ask the owner in chat"; the owner said "pin exact address yes" and nothing
changed, because prose never reaches the guard. Now the gate raises a typed
``token_identity`` ask on the durable asks store; ``/pending`` lists each
candidate; Approve writes an owner_approved binding, Reject records NOT trusted
and quarantines the held look-alike.
"""
import types

import pytest

from agents.task.goals.board import GoalBoard
from core import open_positions as op
from core.wallet import token_pins
from core.wallet import token_provenance as tp
from tools.controller import approval_queue as aq
from tools.defi import token_identity_ask as tia
from tools.defi.identity_gate import buy_identity_refusal

REAL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"
OTHER = "0x" + "4" * 40


@pytest.fixture
def board(tmp_path, monkeypatch):
    tp._reset_for_tests()
    monkeypatch.setattr(tp, "_PROBES", {})
    monkeypatch.setattr(tp, "provenance_db_path",
                        lambda data_home=None: str(tmp_path / "wallet" / "prov.db"))
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")
    pins = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: pins)
    book = str(tmp_path / "open_positions.db")
    monkeypatch.setattr(op, "open_positions_db_path", lambda data_dir=None: book)
    op.apply_delta("rob", op.PositionDelta(chain="robinhood", address=FAKE.lower(),
                                           symbol="PNL", qty=33_700_000.0,
                                           cost_usd=134.54))
    b = GoalBoard(str(tmp_path / "goals.db"))
    monkeypatch.setattr(tia, "_board", lambda container=None: b)
    yield b
    tp._reset_for_tests()


def _ctx():
    return types.SimpleNamespace(user_id="rob", metadata={}, role="orchestrator",
                                 is_sub_agent=False)


def _buy(token=REAL, usd=50.0, symbol="PNL"):
    ident = types.SimpleNamespace(symbol=symbol, name="Rob Track Record",
                                  verified=False, source="frozen")
    return buy_identity_refusal(chain="robinhood", token_out=token, id_out=ident,
                                max_spend_usd=usd, route_verdict="UNAVAILABLE",
                                execution_context=_ctx())


def _open_ids(board):
    return [a.id for a in board.asks(user_id="rob", status="open")]


def test_a_collision_with_no_trusted_side_raises_one_ask(board):
    why = _buy()
    assert why and "/pending" in why and "do NOT call owner_ask" in why
    assert "polyrob" not in why
    asks = board.asks(user_id="rob", status="open")
    assert len(asks) == 1
    p = asks[0].payload
    assert p["ask_kind"] == "token_identity" and p["chain"] == "robinhood"
    assert {c["address"].lower() for c in p["candidates"]} == {REAL.lower(), FAKE.lower()}
    held = next(c for c in p["candidates"] if c["address"].lower() == FAKE.lower())
    assert "134.54" in held["provenance"]
    # the next cycle refreshes it — one ask, "already asked"
    again = _buy()
    assert "already asked" in again and len(_open_ids(board)) == 1


def test_an_unverified_ticket_above_the_cap_asks_about_that_contract(board):
    why = _buy(token=OTHER, symbol="MEME", usd=20.0)
    assert why and "/pending" in why
    [ask] = board.asks(user_id="rob", status="open")
    assert [c["address"] for c in ask.payload["candidates"]] == [OTHER]
    assert _buy(token=OTHER, symbol="MEME", usd=5.0) is None  # the scouting ticket


def test_pending_lists_each_candidate_and_bulk_approve_holds_them(board):
    _buy()
    pending = aq.all_pending(user_id="rob", home_dir=str(board.db_path).rsplit("/", 1)[0],
                             instance_id="t", board=board)
    items = [it for it in pending.items if it["kind"] == "token_identity"]
    assert len(items) == 2
    assert all("Approve = trust it" in it["preview"] for it in items)
    assert all(aq.needs_individual_decision(it) for it in items)


def test_approve_trusts_the_candidate_quarantines_the_lookalike_and_closes(board):
    _buy()
    items = tia.list_pending_items(board, "rob")
    real_item = next(it for it in items if REAL.lower() in it["preview"].lower())
    ok, msg = aq.decide_pending("token_identity", real_item["id"], approve=True,
                                user_id="rob", home_dir="/nonexistent",
                                instance_id="t", board=board)
    assert ok, msg
    assert token_pins.owner_pin("robinhood", REAL)["source"] == "owner_approved"
    assert op.get_position("rob", "robinhood", FAKE).status == "quarantined"
    assert _open_ids(board) == []
    assert _buy() is None  # the next buyback passes


def test_reject_records_not_trusted_and_closes_when_all_decided(board):
    _buy()
    items = tia.list_pending_items(board, "rob")
    fake_item = next(it for it in items if FAKE.lower() in it["preview"].lower())
    ok, msg = aq.decide_pending("token_identity", fake_item["id"], approve=False,
                                user_id="rob", home_dir="/x", instance_id="t", board=board)
    assert ok, msg
    assert token_pins.rejection("robinhood", FAKE) is not None
    assert op.get_position("rob", "robinhood", FAKE).status == "quarantined"
    assert len(_open_ids(board)) == 1  # the real one is still undecided
    [left] = tia.list_pending_items(board, "rob")
    aq.decide_pending("token_identity", left["id"], approve=False, user_id="rob",
                      home_dir="/x", instance_id="t", board=board)
    assert _open_ids(board) == []


def test_another_tenant_cannot_decide_it(board):
    _buy()
    item = tia.list_pending_items(board, "rob")[0]
    ok, _ = tia.decide_item(board, item["id"], approve=True, user_id="mallory")
    assert not ok and token_pins.all_pins() == []


def test_generic_ask_lists_skip_it():
    from core.goal_vocab import has_own_surface
    assert has_own_surface({"ask_kind": "token_identity"})
    assert has_own_surface({"ask_kind": "tool_approval"})
    assert not has_own_surface({"origin": "agent"})


def test_the_notice_carries_two_taps_per_candidate(board):
    _buy()
    [ask] = board.asks(user_id="rob", status="open")
    text = tia.render_notice(ask)
    from core.surfaces.actions import notice_actions
    actions = notice_actions("token_identity", text)
    assert len(actions) == 4
    assert "polyrob" not in text


def test_owner_ask_about_the_same_contract_is_not_raised_twice(board):
    _buy()
    ask = tia.open_ask_naming(board, "rob", f"Which PNL is real, {FAKE} or {REAL}?")
    assert ask is not None
    assert tia.open_ask_naming(board, "rob", "Should I rebalance the treasury?") is None
