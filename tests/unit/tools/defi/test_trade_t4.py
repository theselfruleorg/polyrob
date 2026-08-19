"""defi_trade T4 — approve / revoke / swap (proposal 023 T4).

These are money verbs. The tests pin the REFUSALS, because every one of them is
a way real funds leave and do not come back:

  * an unlimited approval outlives the trade as a standing claim on the wallet;
  * a swap without an allowance reverts and burns gas;
  * a swap whose route disagrees with an independent price is being routed
    through a pool someone seeded;
  * dry_run must be the default on every one of them.
"""
import contextlib
import time

import pytest

from core.wallet.tx_guard import Decision
from tools.defi.providers.univ3 import SwapQuote
from tools.defi.trade_tool import (ApproveParams, DefiTradeTool, RevokeParams,
                                   SwapParams)

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
#: Ethereum mainnet USDC — a DIFFERENT contract from Base's at a different
#: address, which is the point of the multi-chain tests below.
ETH_USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


class _Gate:
    def __init__(self):
        self.recorded = []

    @contextlib.asynccontextmanager
    async def reserve(self):
        yield

    def record(self, **kw):
        self.recorded.append(kw)


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
        self.built = None
        _Rail.last = self

    def build_call(self, *, to, data, value=0):
        self.built = {"to": to, "data": data, "value": value}
        return self.built

    def sign_and_send(self, tx):
        self.sent = True
        return "0x" + "cd" * 32

    def await_receipt(self, tx_hash, **kw):
        from core.wallet.broadcast.evm import Receipt
        return Receipt(tx_hash=tx_hash, status="success", block_number=1,
                       gas_used=50000)


#: Per-token prices that make the default _quote() route AGREE with the
#: independent price (1 USDC in -> 0.0005 WETH out implies $2000/WETH). The
#: route check BLOCKS on disagreement, so a single scalar price for both
#: sides would refuse every swap fixture.
_PRICES = {USDC: 1.0, WETH: 2000.0}


def _tool(*, allow=True, quote=None, price=None, captured=None):
    gate = _Gate()

    def _guard(intent, tx, **kw):
        if captured is not None:
            captured.append(intent)
        return Decision(allowed=allow, reason="test", lane="autonomous",
                        amount_usd=1.0)

    if price is None:
        price_fn = lambda c, a: _PRICES.get(a)          # noqa: E731
    elif isinstance(price, dict):
        price_fn = lambda c, a: price.get(a)            # noqa: E731
    else:
        price_fn = lambda c, a: price                   # noqa: E731

    return DefiTradeTool(
        wallet=_Wallet(gate), rail_factory=_Rail, guard_fn=_guard,
        price_fn=price_fn,
        quote_fn=(lambda *a, **k: quote) if quote is not None else (lambda *a, **k: None),
    ), gate


def _quote(amount_out=500_000_000_000_000, fee=500):
    return SwapQuote(chain="base", token_in=USDC, token_out=WETH,
                     amount_in_raw=1_000_000, amount_out_raw=amount_out,
                     fee_tier=fee, router=ROUTER, quoted_at=time.time())


# --- approve ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_refuses_an_effectively_unlimited_allowance():
    """The classic infinite approval (MAX_UINT256-scale)."""
    tool, _ = _tool()
    res = await tool.approve_token(ApproveParams(
        token=USDC, spender=ROUTER, amount=1e60, max_spend_usd=1.0, dry_run=True))
    assert res.error and "UNLIMITED" in res.error
    assert _Rail.last is None or not getattr(_Rail.last, "sent", False)


@pytest.mark.asyncio
async def test_approve_refuses_an_amount_worth_more_than_declared():
    """The infinite marker alone is not enough: 1e30 USDC is absurd but sits far
    below 2**128, so it slipped through until the USD bound was added. An
    allowance is a claim on funds and belongs under the same ceiling as a spend."""
    tool, _ = _tool(price=1.0)
    res = await tool.approve_token(ApproveParams(
        token=USDC, spender=ROUTER, amount=1e30, max_spend_usd=1.0, dry_run=True))
    assert res.error and "at risk" in res.error
    assert "max_spend_usd" in res.error


@pytest.mark.asyncio
async def test_approve_allows_an_amount_within_the_declared_ceiling():
    tool, _ = _tool(price=1.0)
    res = await tool.approve_token(ApproveParams(
        token=USDC, spender=ROUTER, amount=1.0, max_spend_usd=2.0, dry_run=True))
    assert res.error is None
    assert "DRY RUN" in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_approve_declares_the_grant_so_the_guard_can_verify_it():
    """An allowance the simulation reveals but the intent did not declare is
    refused by tx_guard. Declaring it is what makes the approval checkable."""
    captured = []
    tool, _ = _tool(price=1.0, captured=captured)
    await tool.approve_token(ApproveParams(
        token=USDC, spender=ROUTER, amount=1.0, max_spend_usd=2.0, dry_run=True))
    assert captured, "guard was never consulted"
    grants = captured[0].expected_allowance_grants
    assert len(grants) == 1
    token, spender, amount = grants[0]
    assert token.lower() == USDC.lower()
    assert spender.lower() == ROUTER.lower()
    assert amount == 1_000_000  # 1.0 USDC at 6 decimals


@pytest.mark.asyncio
async def test_approve_defaults_to_dry_run_and_does_not_broadcast():
    tool, gate = _tool(price=1.0)
    res = await tool.approve_token(ApproveParams(
        token=USDC, spender=ROUTER, amount=1.0, max_spend_usd=2.0))
    assert "DRY RUN" in (res.extracted_content or "")
    assert not _Rail.last.sent
    assert gate.recorded == []


# --- revoke ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_revoke_encodes_a_zero_allowance():
    tool, _ = _tool()
    await tool.revoke_approval(RevokeParams(
        token=USDC, spender=ROUTER, dry_run=True))
    data = _Rail.last.built["data"]
    assert data.startswith("0x095ea7b3")           # approve(address,uint256)
    assert int(data[2 + 8 + 64:], 16) == 0          # amount == 0


@pytest.mark.asyncio
async def test_revoke_declares_no_allowance_grant():
    captured = []
    tool, _ = _tool(captured=captured)
    await tool.revoke_approval(RevokeParams(token=USDC, spender=ROUTER, dry_run=True))
    assert captured[0].expected_allowance_grants == ()


# --- swap ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_swap_refuses_when_no_route_exists():
    tool, _ = _tool(quote=None)
    res = await tool.swap(SwapParams(token_in=USDC, token_out=WETH,
                                     amount_in=1.0, max_spend_usd=2.0))
    assert res.error and "no Uniswap V3 route" in res.error


@pytest.mark.asyncio
async def test_swap_refuses_identical_tokens():
    tool, _ = _tool(quote=_quote())
    res = await tool.swap(SwapParams(token_in=USDC, token_out=USDC,
                                     amount_in=1.0, max_spend_usd=2.0))
    assert res.error and "same token" in res.error


@pytest.mark.asyncio
async def test_swap_refuses_without_a_sufficient_allowance(monkeypatch):
    """Broadcasting a swap the router cannot pull just burns gas. The refusal
    names the exact remedy instead."""
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 0)
    tool, _ = _tool(quote=_quote())
    res = await tool.swap(SwapParams(token_in=USDC, token_out=WETH,
                                     amount_in=1.0, max_spend_usd=2.0))
    assert res.error and "insufficient allowance" in res.error
    assert "approve_token" in res.error


@pytest.mark.asyncio
async def test_swap_bounds_slippage_into_amount_out_minimum(monkeypatch):
    """amountOutMinimum is the ONLY on-chain protection against a bad fill —
    the swap must revert rather than accept a worse price."""
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    out = 1_000_000_000_000_000_000
    tool, _ = _tool(quote=_quote(amount_out=out))
    await tool.swap(SwapParams(token_in=USDC, token_out=WETH, amount_in=1.0,
                               max_spend_usd=2.0, slippage_bps=100, dry_run=True))
    data = _Rail.last.built["data"]
    # word 6 of exactInputSingle is amountOutMinimum
    min_out = int(data[2 + 8 + 64 * 5: 2 + 8 + 64 * 6], 16)
    assert min_out == out * 9900 // 10000


@pytest.mark.asyncio
async def test_swap_defaults_to_dry_run(monkeypatch):
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    tool, gate = _tool(quote=_quote())
    res = await tool.swap(SwapParams(token_in=USDC, token_out=WETH,
                                     amount_in=1.0, max_spend_usd=2.0))
    assert "DRY RUN" in (res.extracted_content or "")
    assert not _Rail.last.sent
    assert gate.recorded == []


@pytest.mark.asyncio
async def test_swap_targets_the_router_not_the_token(monkeypatch):
    """A swap is a call TO the router. Sending it to the token contract would
    do nothing and lose the gas."""
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    tool, _ = _tool(quote=_quote())
    await tool.swap(SwapParams(token_in=USDC, token_out=WETH, amount_in=1.0,
                               max_spend_usd=2.0, dry_run=True))
    assert _Rail.last.built["to"].lower() == ROUTER.lower()
    assert _Rail.last.built["data"].startswith("0x04e45aaf")


@pytest.mark.asyncio
async def test_swap_declares_no_allowance_grant(monkeypatch):
    """The swap spends; it must not grant. Any allowance the simulation reveals
    is then undeclared, and the guard refuses it."""
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    captured = []
    tool, _ = _tool(quote=_quote(), captured=captured)
    await tool.swap(SwapParams(token_in=USDC, token_out=WETH, amount_in=1.0,
                               max_spend_usd=2.0, dry_run=True))
    assert captured[0].expected_allowance_grants == ()


@pytest.mark.asyncio
async def test_a_refused_guard_broadcasts_nothing(monkeypatch):
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    tool, gate = _tool(allow=False, quote=_quote())
    res = await tool.swap(SwapParams(token_in=USDC, token_out=WETH, amount_in=1.0,
                                     max_spend_usd=2.0, dry_run=False))
    assert "NOT SENT" in (res.extracted_content or "")
    assert not _Rail.last.sent
    assert gate.recorded == []


# --- route sanity ----------------------------------------------------------

def test_route_check_reads_unavailable_when_a_price_is_missing():
    """An unavailable screener must never read as 'route verified'."""
    tool = DefiTradeTool(price_fn=lambda c, a: None)
    ident = type("I", (), {"decimals": 18, "symbol": "X"})()
    verdict, note = tool._route_sanity("base", _quote(), ident, ident)
    assert verdict == "UNAVAILABLE"
    assert "UNAVAILABLE" in note and "AGREES" not in note


@pytest.mark.asyncio
async def test_swap_refuses_when_the_route_disagrees_with_the_independent_price(monkeypatch):
    """§1.2: the route check must BLOCK, not narrate. A quote implying a price
    far from the independent one means the pool is thin or manipulated — a
    number anyone with capital can seed. Half the quoted output at the same
    input is a 100% drift; nothing may broadcast."""
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    # 1 USDC -> 0.00025 WETH implies $4000/WETH vs the independent $2000.
    tool, gate = _tool(quote=_quote(amount_out=250_000_000_000_000))
    res = await tool.swap(SwapParams(token_in=USDC, token_out=WETH,
                                     amount_in=1.0, max_spend_usd=2.0,
                                     dry_run=False))
    assert res.error and "DISAGREES" in res.error
    assert _Rail.last is None or not _Rail.last.sent
    assert gate.recorded == []


@pytest.mark.asyncio
async def test_swap_proceeds_with_a_loud_caution_when_no_independent_price(monkeypatch):
    """No independent price is UNKNOWN, not a refusal at this layer: the spend
    side is still priced and capped by the guard (an unpriceable token_in
    refuses there). The caution must be in the header the agent sees."""
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    tool, _ = _tool(quote=_quote(), price={USDC: 1.0, WETH: None})
    res = await tool.swap(SwapParams(token_in=USDC, token_out=WETH,
                                     amount_in=1.0, max_spend_usd=2.0))
    assert res.error is None
    assert "UNAVAILABLE" in (res.extracted_content or "")


# --- chain confinement ------------------------------------------------------

@pytest.mark.asyncio
async def test_every_money_verb_refuses_a_read_only_chain():
    """Arbitrum is readable (a venue settles there) but nothing about its money
    path is verified, so no value may move on it. Was "only base is supported";
    the door now asks the registry, and the refusal names THAT chain rather
    than pointing at another one."""
    from tools.defi.trade_tool import TransferParams
    tool, _ = _tool(quote=_quote())
    calls = [
        tool.transfer(TransferParams(chain="arbitrum", token=USDC, to=ROUTER,
                                     amount=1.0, max_spend_usd=1.0)),
        tool.approve_token(ApproveParams(chain="arbitrum", token=USDC,
                                         spender=ROUTER, amount=1.0,
                                         max_spend_usd=1.0)),
        tool.revoke_approval(RevokeParams(chain="arbitrum", token=USDC,
                                          spender=ROUTER)),
        tool.swap(SwapParams(chain="arbitrum", token_in=USDC, token_out=WETH,
                             amount_in=1.0, max_spend_usd=1.0)),
    ]
    for coro in calls:
        res = await coro
        assert res.error and "arbitrum" in res.error, res.error


@pytest.mark.asyncio
async def test_every_money_verb_refuses_an_unknown_chain():
    from tools.defi.trade_tool import TransferParams
    tool, _ = _tool(quote=_quote())
    res = await tool.transfer(TransferParams(
        chain="nosuchchain", token=USDC, to=ROUTER, amount=1.0, max_spend_usd=1.0))
    assert res.error and "nosuchchain" in res.error


@pytest.mark.asyncio
async def test_ethereum_reaches_the_guard():
    """The positive half of multi-chain: a verified chain is no longer stopped
    at the door, and the intent carries ITS chain to the guard.

    (The RPC-pin precondition is tx_guard's, not the door's — proven against
    the real guard in test_trade_real_guard.py.)"""
    captured = []
    tool, _ = _tool(quote=_quote(), captured=captured)
    res = await tool.approve_token(ApproveParams(
        chain="ethereum", token=ETH_USDC, spender=ROUTER, amount=1.0,
        max_spend_usd=1.0))
    assert res.error is None, res.error
    assert captured and captured[0].chain == "ethereum"
    assert captured[0].token == ETH_USDC, "the intent must carry ethereum's own USDC"


@pytest.mark.asyncio
async def test_a_swap_on_a_chain_without_a_dex_names_the_missing_route(monkeypatch):
    monkeypatch.setenv("DEFI_EVM_RPC_ROBINHOOD", "https://pinned.example/rpc")
    tool, _ = _tool(quote=_quote())
    res = await tool.swap(SwapParams(chain="robinhood", token_in=USDC,
                                     token_out=WETH, amount_in=1.0,
                                     max_spend_usd=1.0))
    assert res.error and "robinhood" in res.error


def test_the_chain_field_teaches_the_agent_how_to_choose():
    """Owner directive (2026-08-15): the agent picks the chain per task, which
    only works if the trade-offs are in front of it at the point of choice."""
    from tools.defi.trade_tool import SwapParams as SP
    described = SP.model_fields["chain"].description
    assert "ethereum" in described and "base" in described
    assert "gas" in described.lower()


# --- quote freshness (§1.3) -------------------------------------------------

@pytest.mark.asyncio
async def test_swap_refuses_a_stale_quote(monkeypatch):
    """The freshness window is enforced, not decorative. swap() re-quotes
    inline today so this never trips — it exists so a future split of quote
    and execute (e.g. an owner-approval lane) cannot execute a stale price."""
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    stale = SwapQuote(chain="base", token_in=USDC, token_out=WETH,
                      amount_in_raw=1_000_000, amount_out_raw=500_000_000_000_000,
                      fee_tier=500, router=ROUTER, quoted_at=time.time() - 120)
    tool, _ = _tool(quote=stale)
    res = await tool.swap(SwapParams(token_in=USDC, token_out=WETH,
                                     amount_in=1.0, max_spend_usd=2.0))
    assert res.error and "stale" in res.error.lower()


# --- guard: allowance ops legitimately move zero tokens ---------------------

def _intent(**kw):
    from core.wallet.tx_guard import TxIntent
    base = dict(chain="base", token=USDC, to=ROUTER, amount_raw=0,
                max_spend_usd=1.0, expected_allowance_grants=())
    base.update(kw)
    return TxIntent(**base)


def test_txintent_can_declare_an_allowance_operation():
    """An approve/revoke transfers NOTHING — its risk is the allowance, which is
    declared separately. The structural `amount_raw > 0` rule is a TRANSFER rule;
    applying it to an approval refused every approve outright (caught on prod,
    not by the stubbed-guard unit tests)."""
    assert _intent().is_allowance_op is False
    assert _intent(is_allowance_op=True).is_allowance_op is True


def test_guard_refuses_a_zero_amount_transfer_but_allows_a_zero_amount_approval():
    from core.wallet import tx_guard

    def _never_forged(*a, **k):
        return False

    # A zero-value TRANSFER is still meaningless and still refused.
    d = tx_guard.authorize(_intent(), {"to": USDC, "data": "0x", "value": 0},
                           holder="0x" + "22" * 20, gate=None,
                           execution_context=None, tool_self=None,
                           price_fn=lambda c, a: 1.0, forged_fn=_never_forged)
    assert not d.allowed and "greater than zero" in d.reason

    # The same shape, declared as an allowance op, gets past the structural rule
    # (it is then judged on its simulated allowance delta, not on this check).
    d2 = tx_guard.authorize(
        _intent(is_allowance_op=True,
                expected_allowance_grants=((USDC, ROUTER, 100_000),)),
        {"to": USDC, "data": "0x", "value": 0}, holder="0x" + "22" * 20, gate=None,
        execution_context=None, tool_self=None, price_fn=lambda c, a: 1.0,
        forged_fn=_never_forged)
    assert "greater than zero" not in (d2.reason or "")
