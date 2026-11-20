"""126 wave 4: a flow is ONE call. A live ERC-20 swap and a live v3 deposit make the
exact approval to the PINNED spender first and revoke it after (each leg judged by
the guard), so the owner's seat, a card and the agent each need one step."""
from types import SimpleNamespace

import pytest

from tools.defi import approve_verb


def _legs(monkeypatch, on_approve):
    calls = []

    async def approve(tool, p, ctx=None):
        calls.append(("approve", p.token.lower(), p.spender.lower(), p.dry_run))
        on_approve()
        return SimpleNamespace(error=None, extracted_content="RESULT: success (approve)")

    async def revoke(tool, p, ctx=None):
        calls.append(("revoke", p.token.lower(), p.spender.lower(), p.dry_run))
        return SimpleNamespace(error=None, extracted_content="RESULT: success (revoke)")
    monkeypatch.setattr(approve_verb, "perform_approve_token", approve)
    monkeypatch.setattr(approve_verb, "perform_revoke_approval", revoke)
    return calls


@pytest.mark.asyncio
async def test_a_live_erc20_swap_approves_swaps_and_revokes(monkeypatch):
    import tools.defi.providers.univ3 as u
    from tests.unit.tools.defi.test_trade_t4 import USDC, WETH, _Rail, _quote, _tool
    from tools.defi.trade_tool import SwapParams
    state = {"allow": 0}
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: state["allow"])
    calls = _legs(monkeypatch, lambda: state.update(allow=10 ** 30))
    tool, _ = _tool(quote=_quote())
    res = await tool.swap(SwapParams(chain="base", token_in=USDC, token_out=WETH,
                                     amount_in=1.0, max_spend_usd=2.0, dry_run=False))
    text = str(res.error or res.extracted_content)
    assert [c[0] for c in calls] == ["approve", "revoke"], text
    assert calls[0][1] == USDC.lower() and calls[0][2] == calls[1][2] and not calls[0][3]
    assert "approve leg" in text and "revoke leg" in text and _Rail.last.sent


@pytest.mark.asyncio
async def test_a_swap_dry_run_still_names_the_missing_allowance(monkeypatch):
    import tools.defi.providers.univ3 as u
    from tests.unit.tools.defi.test_trade_t4 import USDC, WETH, _quote, _tool
    from tools.defi.trade_tool import SwapParams
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 0)
    calls = _legs(monkeypatch, lambda: None)
    tool, _ = _tool(quote=_quote())
    res = await tool.swap(SwapParams(chain="base", token_in=USDC, token_out=WETH,
                                     amount_in=1.0, max_spend_usd=2.0))
    assert res.error and "insufficient allowance" in res.error and not calls


@pytest.mark.asyncio
async def test_a_live_v3_deposit_approves_mints_and_revokes(monkeypatch):
    T, R = _v3_world(monkeypatch)
    state = {"allow": 0}
    monkeypatch.setattr(R, "allowance", lambda *a: state["allow"])
    calls = _legs(monkeypatch, lambda: state.update(allow=10 ** 30))
    t, gate = T.tool()
    res = await t.lp_add(T.add(dry_run=False))
    assert res.error is None, res.error
    kinds = [c[0] for c in calls]
    assert kinds == ["approve", "approve", "revoke", "revoke"]
    assert {c[2] for c in calls} == {T.NPM.lower()}
    assert "position #99" in res.extracted_content and gate.recorded


def _v3_world(monkeypatch):
    from tests.unit.tools.defi import test_lp_verbs as T
    from tools.defi import lp_reads as R
    monkeypatch.setenv(T.V.FLAG, "true")
    monkeypatch.setattr(T.V, "_pons_warning", lambda *a: "")
    monkeypatch.setattr(T.dex_registry, "verify_pins", lambda *a: None)
    monkeypatch.setattr(R, "pool_address", lambda *a: T.POOL)
    monkeypatch.setattr(R, "view", lambda rpc, to, spec, args=None:
                        T.HOLDER if spec["name"] == "ownerOf"
                        else 18 if spec["name"] == "decimals" else 10 ** 30)
    monkeypatch.setattr(R, "pool_state", lambda *a: SimpleNamespace(sqrt_price_x96=2 ** 96))
    monkeypatch.setattr(R, "allowance", lambda *a: 10 ** 30)
    return T, R


@pytest.mark.asyncio
async def test_a_liquidity_dry_run_carries_its_typed_value(monkeypatch):
    """126 wave 4: a card bounds its call from the dry run's TYPED value."""
    T, _R = _v3_world(monkeypatch)
    t, _ = T.tool()
    res = await t.lp_add(T.add())
    assert res.error is None and res.metadata == {"simulated_usd": 2, "allowed": True}


@pytest.mark.asyncio
async def test_a_v3_deposit_whose_second_grant_fails_revokes_the_first(monkeypatch):
    """A grant made before the one that failed is no standing claim: it is revoked, said."""
    T, R = _v3_world(monkeypatch)
    monkeypatch.setattr(R, "allowance", lambda *a: 0)
    calls = []

    async def approve(tool, p, ctx=None):
        calls.append(("approve", p.token.lower()))
        if len([c for c in calls if c[0] == "approve"]) == 2:
            return SimpleNamespace(error="refused: daily cap", extracted_content=None)
        return SimpleNamespace(error=None, extracted_content="RESULT: success (approve)")

    async def revoke(tool, p, ctx=None):
        calls.append(("revoke", p.token.lower()))
        return SimpleNamespace(error=None, extracted_content="RESULT: success (revoke)")
    monkeypatch.setattr(approve_verb, "perform_approve_token", approve)
    monkeypatch.setattr(approve_verb, "perform_revoke_approval", revoke)
    t, gate = T.tool()
    res = await t.lp_add(T.add(dry_run=False))
    assert res.error and "Nothing was deposited" in res.error and "revoke leg" in res.error
    assert [c[0] for c in calls] == ["approve", "approve", "revoke"]
    assert calls[2][1] == calls[0][1] and not gate.recorded


@pytest.mark.asyncio
async def test_a_swap_that_raises_after_its_approval_still_revokes(monkeypatch):
    from tools.defi import swap_verb
    calls = _legs(monkeypatch, lambda: None)

    async def _swap(tool, params, ctx, legs):
        legs["granted"], legs["approve_text"] = ("0xtoken", "0xrouter"), "approved"
        raise RuntimeError("ledger write failed")
    monkeypatch.setattr(swap_verb, "_swap", _swap)
    p = SimpleNamespace(chain="base")
    res = await swap_verb.perform_swap(object(), p)
    assert res.error and "outcome unknown" in res.error and "revoke leg" in res.error
    assert [c[0] for c in calls] == ["revoke"] and calls[0][1:3] == ("0xtoken", "0xrouter")


@pytest.mark.asyncio
async def test_a_swap_that_used_the_whole_allowance_sends_no_revoke(monkeypatch):
    import tools.defi.providers.univ3 as u
    from tools.defi import swap_verb
    calls = _legs(monkeypatch, lambda: None)
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 0)

    async def _swap(tool, params, ctx, legs):
        legs.update(granted=("0xtoken", "0xrouter"), approve_text="approved", owner="0xme")
        return SimpleNamespace(error=None, extracted_content="RESULT: CONFIRMED", metadata=None)
    monkeypatch.setattr(swap_verb, "_swap", _swap)
    res = await swap_verb.perform_swap(object(), SimpleNamespace(chain="base"))
    assert not calls and "none needed" in res.extracted_content and "STAYS" not in res.extracted_content


@pytest.mark.asyncio
async def test_a_v4_seed_whose_grant_fails_names_the_grants_it_made(monkeypatch):
    """The grants made before the failed one stay for a retry; the refusal names them."""
    from tools.defi import lp_v4_seed, lp_verbs

    async def make_grants(tool, p, ctx):
        return "the permit2 grant of B was not made: refused", ["RESULT: success (grant A)"]
    monkeypatch.setattr(lp_v4_seed, "make_grants", make_grants)

    class _T:
        @staticmethod
        def _ar(error=None, content=None, metadata=None):
            return SimpleNamespace(error=error, extracted_content=content)
    res = await lp_verbs.perform_lp_add(_T(), SimpleNamespace(
        seed="s0123456789", protocol="v4", dry_run=False, chain="base"))
    assert res.error and "RESULT: NOT SENT" in res.error
    assert "STAY" in res.error and "grant A" in res.error
