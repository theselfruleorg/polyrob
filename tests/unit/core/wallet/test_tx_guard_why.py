"""122 W1: an autonomous money act states its reason; the notice shows it with the lane."""
import asyncio
from types import SimpleNamespace

import pytest

from core import act_reason
from core.wallet import tx_guard
from core.wallet import tx_notify as tn

TOKEN, TO, HOLDER = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("DEFI_AUTONOMOUS_TURN_TRADING", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "local")
    monkeypatch.setattr(tx_guard, "_decimals_for", lambda _c, _t: 6)
    import importlib
    money_regime = importlib.import_module("core.config_policy.money_regime")
    monkeypatch.setattr(money_regime, "unattended_money_allowed", lambda: True)


def _authorize(*, why=None, forged=True, ctx="agent", dry_run=False, meta=None):
    from core.wallet.policy import PolicyGate
    from core.wallet.simulation import Deltas
    intent = tx_guard.TxIntent(chain="base", token=TOKEN, to=TO, amount_raw=1_000_000,
                               max_spend_usd=5.0, idempotency_key="k", why=why)
    return tx_guard.authorize(
        intent, {"to": TO, "data": "0x"}, holder=HOLDER,
        gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=10.0),
        execution_context=(SimpleNamespace(user_id="local", metadata=dict(meta or {}))
                           if ctx == "agent" else None),
        tool_self=None, halted_fn=lambda: False,
        forged_fn=lambda _ec, _ts: forged, autonomous_ok_fn=lambda _ec, _ts: True,
        rpc_is_pinned_fn=lambda _c: True,
        simulate_fn=lambda **kw: Deltas(ok=True, token_deltas={TOKEN: -1_000_000},
                                        gas_used=100_000),
        price_fn=lambda _c, _t: 1.0, dry_run=dry_run)


def test_an_autonomous_act_with_no_reason_refuses_and_says_how_to_fix_it():
    d = _authorize()
    assert not d.allowed and d.reason == act_reason.WHY_REFUSAL
    assert "`why`" in d.reason


def test_a_dry_run_is_held_to_the_same_rule():
    assert _authorize(dry_run=True).reason == act_reason.WHY_REFUSAL


def test_a_reason_on_the_intent_lets_it_run_on_the_autonomous_lane():
    d = _authorize(why="take profit at the target")
    assert d.allowed and d.lane == "autonomous"


def test_the_reason_the_controller_bound_reaches_the_intent():
    tok = act_reason.set_act_why("the job's rule: buy below the floor")
    try:
        d = _authorize()
    finally:
        act_reason.reset_act_why(tok)
    assert d.allowed and d.lane == "autonomous"


def test_a_bare_internal_call_is_not_judged_here():
    # No context: the remote signer's re-check (the agent side held the act already).
    d = _authorize(ctx=None, forged=False)
    assert d.reason != act_reason.WHY_REFUSAL


def test_the_notice_shows_the_reason_and_the_lane():
    text = tn.render_broadcast(tn.TxNotice(verb="swap", route="base", amount_in="5 USDC",
                                           usd=5.0, tx_ref="0xabc", lane="autonomous",
                                           why="take profit at the target"))
    assert "why: `take profit at the target` · on its own" in text
    owner = tn.render_settled(tn.TxNotice(verb="swap", route="base", tx_ref="0xabc",
                                          state=tn.STATE_CONFIRMED, lane="owner_direct"))
    assert "why:" not in owner          # the owner's own act: the owner is the reason


def test_a_notice_takes_the_bound_reason_and_records_it(monkeypatch):
    seen = {}
    monkeypatch.setattr(tn, "_emit_event",
                        lambda uid, n, **kw: seen.setdefault("why", n.why))

    async def deliver(container, uid, text, **kw):
        seen["text"] = text
        return "sent"
    import core.surfaces.user_delivery as ud
    monkeypatch.setattr(ud, "deliver_user_message", deliver)
    tok = act_reason.set_act_why("rebalance to the target")
    try:
        asyncio.run(tn.notify(None, "local", tn.TxNotice(
            verb="swap", route="base", tx_ref="0xabc", state=tn.STATE_CONFIRMED,
            lane="autonomous"), settled=True))
    finally:
        act_reason.reset_act_why(tok)
    assert seen["why"] == "rebalance to the target"
    assert "why: `rebalance to the target` · on its own" in seen["text"]


def _grant(mandate=""):
    g = {"approved": True, "max_spend_usd": 5.0}
    if mandate:
        g["mandate"] = mandate
    return {tx_guard.OWNER_GRANT_KEY: g}


def test_a_mandate_act_is_held_to_the_autonomous_why_rule():
    """Audit 2026-11-21 L-3: a mandate act rides the owner_approved lane, yet the agent
    decided it; it states its reason. A card the owner tapped needs none."""
    d = _authorize(meta=_grant("m_0011aabb"))
    assert not d.allowed and d.reason == act_reason.WHY_REFUSAL
    d = _authorize(meta=_grant("m_0011aabb"), why="mint inside the trust band")
    assert d.allowed and d.lane == "owner_approved"
    assert _authorize(meta=_grant()).allowed


def test_a_mandate_act_is_recorded_as_the_mandates_act():
    meta = _grant("m_0011aabb")
    assert act_reason.act_source("goal-1", autonomous=True, metadata=meta) == "mandate m_0011aabb"
    assert act_reason.grant_mandate(meta) == "m_0011aabb"
    assert act_reason.grant_mandate(_grant()) == ""


def test_the_nft_mint_reveal_buy_and_x402_fetch_take_a_why():
    from tools.agent_nft.tool import BuyParams, MintParams, RevealParams
    from tools.x402.service import FetchParams
    for model in (MintParams, RevealParams, BuyParams, FetchParams):
        assert "why" in model.model_fields, model.__name__
    assert MintParams(chain="robinhood", max_spend_usd=5, why="the sale opened").why == "the sale opened"
