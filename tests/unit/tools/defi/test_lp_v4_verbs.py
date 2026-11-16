"""approve_token(via='permit2'): one exact, expiring Permit2 grant to the pinned
v4 PositionManager (core handoff W6; 048 phase 3)."""
import time
from types import SimpleNamespace

import pytest

from core.wallet import abi, dex_registry, liquidity_guard
from core.wallet.tx_guard import Decision
from tools.defi import lp_v4_verbs
from tools.defi.trade_tool import ApproveParams, DefiTradeTool
from tests.unit.tools.defi.test_deploy_and_call_verbs import _Gate, _Rail, _Wallet

TKN = "0x5555555555555555555555555555555555555555"
ROW = dex_registry.row_for("robinhood", "v4")


@pytest.fixture
def tool(monkeypatch):
    captured = []

    def guard(intent, tx, **kw):
        captured.append((intent, tx))
        return Decision(True, "test", "autonomous", 1.0, 60000)
    monkeypatch.setattr(dex_registry, "verify_pins", lambda *a: None)
    import core.wallet.tokens as tokens
    monkeypatch.setattr(tokens, "get_token_identity",
                        lambda chain, t: SimpleNamespace(decimals=18, symbol="TKN"))
    t = DefiTradeTool(wallet=_Wallet(_Gate()), rail_factory=_Rail, guard_fn=guard,
                      price_fn=lambda *a: 1e-5, fallback_price_fn=lambda *a: None)
    t._notify_tx = lambda *a, **kw: None
    return t, captured


def _params(**kw):
    return ApproveParams(**(dict(chain="robinhood", token=TKN, spender=ROW.position_manager,
                                 amount=1000.0, max_spend_usd=5.0, via="permit2") | kw))


@pytest.mark.asyncio
async def test_permit2_grant_is_the_exact_expiring_approve(tool):
    t, captured = tool
    res = await t.approve_token(_params())
    assert res.error is None, res.error
    intent, tx = captured[0]
    assert tx["to"] == ROW.permit2
    assert tx["data"].startswith(abi.selector("approve(address,address,uint160,uint48)"))
    assert intent.is_allowance_op and intent.to == ROW.position_manager
    assert intent.expected_allowance_grants == ((TKN, ROW.position_manager, 1000 * 10 ** 18),)
    _tok, _sp, _amt, exp = abi.decode(
        [{"type": "address"}, {"type": "address"}, {"type": "uint160"}, {"type": "uint48"}],
        "0x" + tx["data"][10:])
    assert time.time() < exp <= time.time() + lp_v4_verbs.PERMIT2_GRANT_TTL_S + 5
    # and the guard's rule recognises exactly this grant
    assert liquidity_guard.permit2_grant_declared(
        intent, tx, ROW.permit2, TKN, ROW.position_manager, 1000 * 10 ** 18)


@pytest.mark.asyncio
async def test_permit2_grant_to_any_other_spender_refuses(tool):
    t, captured = tool
    res = await t.approve_token(_params(spender="0x" + "3" * 40))
    assert "only the pinned v4" in res.error and not captured


@pytest.mark.asyncio
async def test_permit2_on_a_chain_without_a_pinned_v4_refuses(tool):
    t, captured = tool
    res = await t.approve_token(_params(chain="solana"))
    assert res.error and not captured


def test_a_reverted_lp_add_leaves_the_lp_caps(monkeypatch):
    """098 review (TS-23): a reverted add moved nothing; it no longer fills
    LP_ETH_CAP / LP_ETH_DAILY_CAP."""
    import time as _t
    from types import SimpleNamespace
    from core.money.receipt_state import voided_refs
    from tools.defi.lp_v4_verbs import caps_refusal
    monkeypatch.setenv("LP_ETH_CAP", "1")
    monkeypatch.setenv("LP_ETH_DAILY_CAP", "1")
    monkeypatch.setenv("LP_ETH_FLOOR", "0")
    now = _t.time()
    audit = [{"action": "lp_add", "native_raw": str(10 ** 18), "ts": now, "result_ref": "0xa",
              "receipt_state": "pending_receipt"},
             {"action": "receipt", "result_ref": "0xa", "receipt_state": "reverted", "ts": now}]
    gate = SimpleNamespace(audit_log=audit, _voided=lambda: voided_refs(audit))
    assert caps_refusal(gate, 10 ** 17, 10 ** 19, now=now) is None
    gate_live = SimpleNamespace(audit_log=audit[:1], _voided=lambda: set())
    assert "LP_ETH_CAP" in caps_refusal(gate_live, 10 ** 17, 10 ** 19, now=now)


def test_a_withdrawal_returns_its_native_to_the_program_cap(monkeypatch):
    """118 W1: LP_ETH_CAP is the NET deposit. A booked v4 withdrawal returns its
    declared native minimum, in time order and never below zero; the daily bound
    stays gross."""
    import time as _t
    from types import SimpleNamespace
    from tools.defi.lp_v4_verbs import caps_refusal
    monkeypatch.setenv("LP_ETH_CAP", "1")
    monkeypatch.setenv("LP_ETH_DAILY_CAP", "0")
    monkeypatch.setenv("LP_ETH_FLOOR", "0")
    now = _t.time()
    A, B = "erc721:0xnpm:1", "erc721:0xnpm:2"
    add = {"action": "lp_add", "native_raw": str(10 ** 18), "ts": now - 30, "result_ref": "0xa",
           "asset": A}
    back = {"action": "lp_remove", "native_raw": 0, "native_back_raw": 6 * 10 ** 17,
            "ts": now - 20, "result_ref": "0xb", "asset": A}
    gate = lambda rows: SimpleNamespace(audit_log=rows, _voided=lambda: set())
    assert "LP_ETH_CAP" in caps_refusal(gate([add]), 5 * 10 ** 17, 10 ** 19, now=now)
    assert caps_refusal(gate([add, back]), 5 * 10 ** 17, 10 ** 19, now=now) is None
    assert "0.4 ETH net deposited" in caps_refusal(gate([add, back]), 7 * 10 ** 17, 10 ** 19, now=now)
    # A withdrawal BEFORE any booked deposit (a position made outside the ledger)
    # returns nothing: the later deposit still counts in full.
    early = dict(back, ts=now - 60)
    assert "LP_ETH_CAP" in caps_refusal(gate([early, add]), 10 ** 17, 10 ** 19, now=now)
    # A reverted withdrawal returned nothing.
    voided = SimpleNamespace(audit_log=[add, back], _voided=lambda: {"0xb"})
    assert "LP_ETH_CAP" in caps_refusal(voided, 5 * 10 ** 17, 10 ** 19, now=now)
    # PER POSITION: a withdrawal from a position the ledger did not deposit
    # (made outside the agent) returns nothing against another position's deposit.
    other = dict(back, asset=B, native_back_raw=10 ** 18)
    assert "LP_ETH_CAP" in caps_refusal(gate([add, other]), 5 * 10 ** 17, 10 ** 19, now=now)
    # A withdrawal whose receipt is not read yet returns nothing, and neither
    # does one whose position is not known.
    pending = dict(back, receipt_state="pending_receipt")
    assert "LP_ETH_CAP" in caps_refusal(gate([add, pending]), 5 * 10 ** 17, 10 ** 19, now=now)
    unkeyed = dict(back, asset=None)
    assert "LP_ETH_CAP" in caps_refusal(gate([add, unkeyed]), 5 * 10 ** 17, 10 ** 19, now=now)
    # An add whose position is not known stays counted in full.
    blind = dict(add, asset=None)
    assert "LP_ETH_CAP" in caps_refusal(gate([blind, back]), 5 * 10 ** 17, 10 ** 19, now=now)
    # `returning`: the withdrawal a move sends first, netted by the same rule.
    assert caps_refusal(gate([add]), 5 * 10 ** 17, 10 ** 19, now=now,
                        returning=(A, 6 * 10 ** 17)) is None
    assert "LP_ETH_CAP" in caps_refusal(gate([add]), 5 * 10 ** 17, 10 ** 19, now=now,
                                        returning=(B, 6 * 10 ** 17))
    # The daily bound sums the adds only.
    monkeypatch.setenv("LP_ETH_DAILY_CAP", "1")
    assert "LP_ETH_DAILY_CAP" in caps_refusal(gate([add, back]), 5 * 10 ** 17, 10 ** 19, now=now)


@pytest.mark.parametrize("slippage_bps", [0, 100, 990, 991, 1000])
def test_the_signed_maxima_stay_inside_the_guards_price_bound(slippage_bps):
    """Every allowed slippage builds maxima the guard signs: the largest one
    (1000) made maxima above MINT_MAX_GAP_BPS and the mint always refused."""
    from tools.defi import lp_v4 as V
    sqrt_price = 2 ** 96 * 7 // 3
    desired = [3 * 10 ** 18, 5 * 10 ** 21]
    keep = 10000 - slippage_bps
    lo, hi, liq, used0, used1 = V.full_range_quote(
        sqrt_price, 200, desired[0] * keep // 10000, desired[1] * keep // 10000)
    maxima = lp_v4_verbs.signed_maxima(desired, (used0, used1), slippage_bps)
    assert all(m <= d for m, d in zip(maxima, desired))
    liquidity_guard._assert_price_bound(
        {"tick_lower": lo, "tick_upper": hi, "liquidity": liq,
         "amount0_max": maxima[0], "amount1_max": maxima[1]},
        sqrt_price, liquidity_guard.MINT_MAX_GAP_BPS, "the pool price")
    assert lp_v4_verbs.signed_maxima([10, 10], (0, 4), slippage_bps)[0] == 0
