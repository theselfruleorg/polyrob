"""126 wave 1: the 2026-11-20 LP move, end to end — ONE tap, NO typed line.

Then: a declaration card whose Confirm sent nothing, a `/lp seed run <ref> max <usd>
go` line to type, and a Permit2 approval no seat could make. Now the agent's
lp_add(move_from=…) raises ONE call card holding the dry run and the exact call; the
owner's one Confirm declares, grants, withdraws and mints.
"""
import re
from types import SimpleNamespace

import pytest

from core.surfaces import call_cards, cards
from core.wallet import lp_seed as S
from tests.unit.tools.defi.test_lp_v4_move import (  # noqa: F401 — `world` is autouse
    LIQ, TX, _fake_perform, _move, world)
from tests.unit.tools.defi.test_lp_v4_seed import _tool
from tools.controller import call_card_run as RUN
from tools.defi import lp_verbs as LV

#: Any line the owner would have to TYPE (a seat verb with arguments or a go word).
_TYPED = re.compile(r"/lp seed (run|confirm)|\bmax <usd>|\bgo\b — |Send it: /")


def _owner_ctx():
    return SimpleNamespace(user_id="owner", role="orchestrator", is_sub_agent=False,
                           metadata={}, session_id="s-1")


def _fake(world, monkeypatch, **kw):
    """The rails of test_lp_v4_move, but the agent's own proposal call is real."""
    real = LV._perform
    calls = _fake_perform(world, monkeypatch, **kw)
    faked = LV._perform

    async def perform(tool, p, ctx, verb):
        if getattr(p, "move_from", None) is not None:
            return await real(tool, p, ctx, verb)
        return await faked(tool, p, ctx, verb)
    monkeypatch.setattr(LV, "_perform", perform)
    return calls


@pytest.fixture
def tool(monkeypatch):
    monkeypatch.setattr("core.instance.is_owner_uid", lambda uid, **kw: uid == "owner")
    t, _gate = _tool([])
    monkeypatch.setitem(RUN.TOOL_FACTORIES, "defi_trade", lambda: t)
    return t


async def _agent_moves(t):
    res = await t.lp_add(_move(), _owner_ctx())
    assert res.error is None, res.error
    assert getattr(res, "metadata", None) and "owner_act" in res.metadata, vars(res)
    return res


@pytest.mark.asyncio
async def test_the_move_is_one_tap_and_no_typed_line(world, tool, monkeypatch):
    calls = _fake(world, monkeypatch)
    res = await _agent_moves(tool)
    card = world["pushed"][-1]
    assert card.kind == cards.KIND_CALL and card.title == "Move a pool position"
    assert res.metadata["owner_act"]["card_id"] == card.card_id
    assert calls == [("lp_remove", True)]            # the dry run is the card's body
    taps, typed = 0, 0
    for text in (res.extracted_content, card.body):
        typed += len(_TYPED.findall(text))
    p = cards.press(card.card_id, "ok", "owner")
    taps += 1
    out = await RUN.on_press(p, seat="telegram")
    typed += len(_TYPED.findall(out))
    assert (taps, typed) == (1, 0), out
    assert calls == [("lp_remove", True), ("lp_remove", False), ("lp_add", False)]
    done = cards.store().get(card.card_id)
    assert done.state == cards.S_DONE, done.result
    assert "step 2/2 (defi_trade_lp_add): done" in done.result
    ref = card.draft["call"]["steps"][0]["args"]["ref"]
    assert S.get(ref, user_id="owner")["tx_hash"] == "withdraw:" + TX


@pytest.mark.asyncio
async def test_the_card_holds_the_bound_and_runs_what_it_showed(world, tool, monkeypatch):
    _fake(world, monkeypatch)
    await _agent_moves(tool)
    card = world["pushed"][-1]
    run = card.draft["call"]["steps"][1]
    assert run["do"] == "defi_trade_lp_add" and run["args"]["dry_run"] is False
    assert run["args"]["max_spend_usd"] > 0 and "the guard holds the mint to $" in card.body
    # A row whose steps changed is refused, and nothing runs.
    st = cards.store()
    bad = dict(card.draft)
    bad["call"] = dict(bad["call"], steps=[{"do": "defi_trade_lp_add",
                                            "args": dict(run["args"], max_spend_usd=1e9)}])
    st.transition(card.card_id, frozenset({cards.S_OPEN}), cards.S_OPEN, draft=bad)
    p = cards.press(card.card_id, "ok", "owner")
    out = await RUN.on_press(p, seat="telegram")
    assert "do not match what it showed" in out
    assert cards.store().get(card.card_id).state == cards.S_FAILED


@pytest.mark.asyncio
async def test_a_second_tap_runs_nothing(world, tool, monkeypatch):
    calls = _fake(world, monkeypatch)
    await _agent_moves(tool)
    card = world["pushed"][-1]
    await RUN.on_press(cards.press(card.card_id, "ok", "owner"), seat="telegram")
    again = cards.press(card.card_id, "ok", "owner")
    assert again.call is None and "already decided" in again.reply
    assert calls.count(("lp_add", False)) == 1


@pytest.mark.asyncio
async def test_a_mint_that_did_not_go_offers_the_rest_as_one_card(world, tool, monkeypatch):
    _fake(world, monkeypatch, mint=SimpleNamespace(
        error="refused: the guard refused the mint. RESULT: NOT SENT.", extracted_content=None))
    await _agent_moves(tool)
    card = world["pushed"][-1]
    out = await RUN.on_press(cards.press(card.card_id, "ok", "owner"), seat="telegram")
    assert cards.store().get(card.card_id).state == cards.S_FAILED
    nxt = world["pushed"][-1]
    assert nxt.card_id != card.card_id and nxt.kind == cards.KIND_CALL
    assert f"card {nxt.card_id} (one tap)" in out
    assert [s["do"] for s in call_cards.steps_of(nxt)] == ["defi_trade_lp_add"]
    assert nxt.parent_id == card.card_id


@pytest.mark.asyncio
async def test_a_refresh_makes_a_fresh_card_from_a_new_dry_run(world, tool, monkeypatch):
    calls = _fake(world, monkeypatch)
    await _agent_moves(tool)
    card = world["pushed"][-1]
    out = await RUN.on_press(cards.press(card.card_id, "re", "owner"), seat="telegram")
    assert cards.store().get(card.card_id).state == cards.S_REPLACED
    fresh = world["pushed"][-1]
    assert fresh.card_id != card.card_id and fresh.card_id in out
    assert calls == [("lp_remove", True), ("lp_remove", True)]


@pytest.mark.asyncio
async def test_a_card_never_reaches_another_tenant_or_a_leaf(world, tool):
    leaf = SimpleNamespace(user_id="owner", role="leaf", is_sub_agent=True, metadata={})
    res = await RUN.need_owner(tool, leaf, title="t", body="b", steps=[
        {"do": "lp_seed_confirm", "args": {"ref": "x"}}])
    assert res.error and "leaf" in res.error
    other = SimpleNamespace(user_id="someone", role="orchestrator", is_sub_agent=False,
                            metadata={})
    res = await RUN.need_owner(tool, other, title="t", body="b", steps=[
        {"do": "lp_seed_confirm", "args": {"ref": "x"}}])
    assert res.error and "not the owner" in res.error


def test_a_step_is_a_money_action_or_an_owner_write():
    with pytest.raises(ValueError, match="cannot be run from a card"):
        RUN.check_steps([{"do": "shell_run", "args": {"command": "ls"}}])
    with pytest.raises(ValueError, match="cannot be run from a card"):
        RUN.check_steps([{"do": "defi_data_lp_positions", "args": {}}])
    RUN.check_steps([{"do": "defi_trade_lp_add", "args": {"seed": "x"}}])


def test_the_steps_are_plain_values_and_bounded():
    with pytest.raises(ValueError):
        call_cards.clean_steps([{"do": "defi_trade_lp_add", "args": {"x": [1]}}])
    with pytest.raises(ValueError):
        call_cards.clean_steps([])
    with pytest.raises(ValueError):
        call_cards.clean_steps([{"do": "Bad Name", "args": {}}])
