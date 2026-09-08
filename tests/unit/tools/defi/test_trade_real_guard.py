"""DefiTradeTool -> the REAL tx_guard.authorize, end to end (handoff §6.2).

Every other trade test stubs the guard, and that blindness shipped TWO
dead-on-arrival approve paths in a row (the structural amount>0 rule, then the
zero-outflow rule) — each caught only by a prod dry run. These tests run the
tool's verbs through the genuine authorize(): real PolicyGate, real
forged-turn detector, real kill-switch and autonomy-marker probes. Only the
two network boundaries are substituted: the delta engine (a fake
eth_simulateV1 result shaped exactly like a real one) and the RPC pin (env).
"""
import contextlib
import types

import pytest

from core.wallet import simulation
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from tools.defi.providers.univ3 import SwapQuote
from tools.defi.trade_tool import ApproveParams, DefiTradeTool, RevokeParams, SwapParams

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"


class _Signer:
    address = "0x2222222222222222222222222222222222222222"


class _Wallet:
    def __init__(self, gate):
        self.policy = gate

    def operational_signer(self):
        return _Signer()


class _Rail:
    last = None

    def __init__(self, chain, signer, **kw):
        self.sent = False
        self.sent_tx = None
        self.sized_with = None
        _Rail.last = self

    def build_call(self, *, to, data, value=0):
        return {"to": to, "data": data, "value": value, "chainId": 8453,
                "gas": 120_000}

    def build_erc20_transfer(self, *, token, to, amount_raw):
        return {"to": token, "data": "0xa9059cbb", "value": 0, "chainId": 8453,
                "gas": 120_000}

    def size_gas(self, tx, sim_gas_used):
        self.sized_with = sim_gas_used
        return {**tx, "gas": 424_242}

    def sign_and_send(self, tx):
        self.sent = True
        self.sent_tx = tx
        return "0x" + "cd" * 32

    def await_receipt(self, tx_hash, **kw):
        from core.wallet.broadcast.evm import Receipt
        return Receipt(tx_hash=tx_hash, status="success", block_number=1,
                       gas_used=50000)


def _approve_sim(**kw):
    """Deltas shaped exactly like a real approve: zero token movement, the
    declared allowance appears, the Approval event is on the measured pair."""
    tokens, spenders = kw["tokens"], kw["spenders"]
    return Deltas(
        ok=True, native_delta=0,
        token_deltas={t: 0 for t in tokens},
        allowance_deltas={(t, s): 1_000_000 for t in tokens for s in spenders},
        holder_approvals=tuple((t.lower(), s.lower(), 1_000_000)
                               for t in tokens for s in spenders),
    )


def _swap_sim(**kw):
    """Deltas shaped like a real exactInputSingle: token_in outflow, token_out
    INFLOW (the guard watches the declared inflow token since 2026-08-26), the
    watched router allowance DECREASES, no grants, gasUsed measured. Only the
    outflow token emits a Transfer FROM the holder."""
    tokens, spenders = kw["tokens"], kw["spenders"]
    token_in, inflows = tokens[0], tokens[1:]
    deltas = {token_in: -1_000_000}
    deltas.update({t: 500_000_000_000_000 for t in inflows})
    return Deltas(
        ok=True, native_delta=0,
        token_deltas=deltas,
        allowance_deltas={(token_in, s): -1_000_000 for s in spenders},
        holder_transfers=((token_in.lower(), "0x" + "aa" * 20, 1_000_000),),
        gas_used=140_000,
    )


PRICES = {USDC: 1.0, WETH: 2000.0}


def _route_fn(chain, token_in, token_out, amount_in_raw, *, holder, slippage_bps):
    """The REAL UniV3RouteProvider over a stubbed pool quote.

    This suite's whole point is that the guard is real, so the route must be a
    real one too — a hand-built RouteQuote could carry calldata or a spender the
    provider would never produce, and the delta assertions would then be
    asserting against fiction.
    """
    import tools.defi.providers.univ3 as u
    from tools.defi.providers import routes
    from tools.defi.providers.routes.univ3_route import UniV3RouteProvider
    real = u.best_quote
    u.best_quote = lambda *a, **k: SwapQuote(
        chain=chain, token_in=token_in, token_out=token_out,
        amount_in_raw=amount_in_raw, amount_out_raw=500_000_000_000_000,
        fee_tier=500, router=ROUTER, quoted_at=__import__("time").time())
    try:
        return routes.best_route_with_reason(
            chain, token_in, token_out, amount_in_raw, holder=holder,
            slippage_bps=slippage_bps, providers=(UniV3RouteProvider(),))
    finally:
        u.best_quote = real


def _tool(price_fn=None):
    gate = PolicyGate(max_per_tx_usd=2.0, daily_cap_usd=10.0)
    tool = DefiTradeTool(
        wallet=_Wallet(gate), rail_factory=_Rail, guard_fn=None,   # REAL guard
        price_fn=price_fn or (lambda c, a: PRICES.get(a)),
        route_fn=_route_fn,
    )
    return tool, gate


@pytest.fixture(autouse=True)
def _pinned_rpc(monkeypatch):
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", "https://pinned.example/rpc")
    monkeypatch.delenv("DEFI_AUTONOMOUS_MAX_USD", raising=False)
    _Rail.last = None


def _genuine_ctx():
    return types.SimpleNamespace(is_sub_agent=False, role="orchestrator",
                                 metadata={}, session_id="t-e2e-genuine")


def _leaf_ctx():
    return types.SimpleNamespace(is_sub_agent=False, role="leaf",
                                 metadata={}, session_id="t-e2e-leaf")


# --- approve ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_real_approve_clears_the_real_guard_dry_run(monkeypatch):
    """THE test that would have caught both DOA holes: the exact deltas a
    genuine approve produces must clear the genuine authorize()."""
    monkeypatch.setattr(simulation, "simulate", _approve_sim)
    tool, gate = _tool()
    res = await tool.approve_token(
        ApproveParams(token=USDC, spender=ROUTER, amount=1.0, max_spend_usd=2.0),
        execution_context=_genuine_ctx())
    assert res.error is None
    content = res.extracted_content or ""
    assert "DRY RUN" in content, content
    assert "authorized" in content
    # §1.1 closure: the grant is priced (1.0 USDC at $1), not $0.00.
    assert "$1.0000" in content
    assert gate.audit_log == []


@pytest.mark.asyncio
async def test_an_unpriceable_grant_is_refused_by_the_real_guard(monkeypatch):
    """§1.1 end to end: a low-confidence token prices as None. The tool's
    early USD bound silently skips — the guard must still refuse."""
    monkeypatch.setattr(simulation, "simulate", _approve_sim)
    tool, _ = _tool(price_fn=lambda c, a: None)
    res = await tool.approve_token(
        ApproveParams(token=USDC, spender=ROUTER, amount=1.0, max_spend_usd=2.0),
        execution_context=_genuine_ctx())
    content = res.extracted_content or ""
    assert "NOT SENT" in content
    assert "price" in content.lower()


@pytest.mark.asyncio
async def test_a_real_approve_broadcasts_and_records_zero_spend(monkeypatch):
    """2026-08-26 exit untying: an approve is a PRECONDITION, not a spend —
    recording the grant's USD consumed the daily cap the swap then needed,
    charging one ticket twice. The grant is still CHECKED against headroom
    before it lands (tx_guard runs gate.check on its value); only the
    recorded spend is $0 — the swap records the real number."""
    monkeypatch.setattr(simulation, "simulate", _approve_sim)
    tool, gate = _tool()
    res = await tool.approve_token(
        ApproveParams(token=USDC, spender=ROUTER, amount=1.0, max_spend_usd=2.0,
                      dry_run=False),
        execution_context=_genuine_ctx())
    assert res.error is None
    assert "CONFIRMED" in (res.extracted_content or "")
    assert _Rail.last.sent is True
    entry = gate.audit_log[-1]
    assert entry["action"] == "approve"
    assert entry["amount_usd"] == 0.0


@pytest.mark.asyncio
async def test_a_leaf_turn_cannot_approve_through_the_real_detector(monkeypatch):
    """The genuine _is_forged_or_autonomous_turn, not a stub, must refuse a
    delegated leaf turn."""
    monkeypatch.setattr(simulation, "simulate", _approve_sim)
    tool, gate = _tool()
    res = await tool.approve_token(
        ApproveParams(token=USDC, spender=ROUTER, amount=1.0, max_spend_usd=2.0,
                      dry_run=False),
        execution_context=_leaf_ctx())
    content = res.extracted_content or ""
    assert "NOT SENT" in content
    assert "forged" in content or "autonomous" in content
    assert _Rail.last is None or not _Rail.last.sent
    assert gate.audit_log == []


@pytest.mark.asyncio
async def test_an_unpinned_second_chain_refuses_against_the_real_guard(monkeypatch):
    """Multi-chain (2026-08-17): the RPC pin is PER CHAIN. Base being pinned
    must not arm ethereum — the endpoint is the trust anchor for that chain's
    simulation, deltas and caps, and the refusal names ethereum's own variable."""
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", "https://pinned.example/rpc")
    monkeypatch.delenv("DEFI_EVM_RPC_ETHEREUM", raising=False)

    def _boom(**kw):
        raise AssertionError("simulate must not run without a pinned RPC")

    monkeypatch.setattr(simulation, "simulate", _boom)
    tool, _ = _tool()
    res = await tool.approve_token(
        ApproveParams(chain="ethereum",
                      token="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                      spender=ROUTER, amount=1.0, max_spend_usd=2.0),
        execution_context=_genuine_ctx())
    content = (res.extracted_content or "") + (res.error or "")
    assert "NOT SENT" in content or "DEFI_EVM_RPC_ETHEREUM" in content
    assert "DEFI_EVM_RPC_ETHEREUM" in content


@pytest.mark.asyncio
async def test_no_pinned_rpc_refuses_before_simulating(monkeypatch):
    """The exact refusal observed on prod, pinned as a test: without
    DEFI_EVM_RPC_BASE nothing simulates and nothing arms."""
    monkeypatch.delenv("DEFI_EVM_RPC_BASE", raising=False)

    def _boom(**kw):
        raise AssertionError("simulate must not run without a pinned RPC")

    monkeypatch.setattr(simulation, "simulate", _boom)
    tool, _ = _tool()
    res = await tool.approve_token(
        ApproveParams(token=USDC, spender=ROUTER, amount=1.0, max_spend_usd=2.0),
        execution_context=_genuine_ctx())
    content = res.extracted_content or ""
    assert "NOT SENT" in content
    assert "DEFI_EVM_RPC_BASE" in content


# --- revoke ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_real_revoke_clears_the_real_guard_without_a_price(monkeypatch):
    """Revoking an unpriceable (worthless/poisoned) token must always be
    possible — it is the cleanup verb."""
    def _revoke_sim(**kw):
        tokens = kw["tokens"]
        return Deltas(ok=True, native_delta=0,
                      token_deltas={t: 0 for t in tokens},
                      allowance_deltas={},
                      holder_approvals=tuple((t.lower(), ROUTER.lower(), 0)
                                             for t in tokens))

    monkeypatch.setattr(simulation, "simulate", _revoke_sim)
    tool, _ = _tool(price_fn=lambda c, a: None)
    res = await tool.revoke_approval(
        RevokeParams(token=USDC, spender=ROUTER),
        execution_context=_genuine_ctx())
    assert res.error is None
    assert "DRY RUN" in (res.extracted_content or "")


# --- swap ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_real_swap_clears_the_real_guard_dry_run(monkeypatch):
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    monkeypatch.setattr(simulation, "simulate", _swap_sim)
    tool, _ = _tool()
    res = await tool.swap(
        SwapParams(token_in=USDC, token_out=WETH, amount_in=1.0,
                   max_spend_usd=2.0),
        execution_context=_genuine_ctx())
    assert res.error is None, res.error
    content = res.extracted_content or ""
    assert "DRY RUN" in content
    assert "authorized" in content


@pytest.mark.asyncio
async def test_a_broadcast_swap_is_gas_sized_from_the_simulation(monkeypatch):
    """§3a end to end: the simulation's gasUsed rides the Decision out of the
    real authorize(), the tool sizes the tx through the rail, and the SIZED
    tx is what broadcasts — never the fixed default that out-of-gas-reverts
    a 130-190k swap."""
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    monkeypatch.setattr(simulation, "simulate", _swap_sim)   # gas_used=140_000
    tool, _ = _tool()
    res = await tool.swap(
        SwapParams(token_in=USDC, token_out=WETH, amount_in=1.0,
                   max_spend_usd=2.0, dry_run=False),
        execution_context=_genuine_ctx())
    assert res.error is None, res.error
    assert _Rail.last.sent is True
    assert _Rail.last.sized_with == 140_000
    assert _Rail.last.sent_tx["gas"] == 424_242, \
        "the tool must broadcast the SIZED tx, not the originally built one"


@pytest.mark.asyncio
async def test_a_swap_hiding_an_approval_to_an_unmeasured_spender_refuses(monkeypatch):
    """The event-log layer end to end: the reads never measured this pair, so
    only the Approval event witnesses the hidden grant."""
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)

    def _evil_sim(**kw):
        base = _swap_sim(**kw)
        return Deltas(
            ok=True, native_delta=0,
            token_deltas=base.token_deltas,
            allowance_deltas=base.allowance_deltas,
            holder_transfers=base.holder_transfers,
            holder_approvals=((USDC.lower(), "0x" + "66" * 20, 10 ** 30),))

    monkeypatch.setattr(simulation, "simulate", _evil_sim)
    tool, gate = _tool()
    res = await tool.swap(
        SwapParams(token_in=USDC, token_out=WETH, amount_in=1.0,
                   max_spend_usd=2.0, dry_run=False),
        execution_context=_genuine_ctx())
    content = res.extracted_content or ""
    assert "NOT SENT" in content
    assert "Approval" in content
    assert _Rail.last is None or not _Rail.last.sent
    assert gate.audit_log == []
