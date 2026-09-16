"""defi_trade.wrap — native -> wrapped native, the verb that did not exist.

Live, 2026-09-13. The treasury held 0.0512 native ETH on Robinhood Chain and
needed WETH to trade. Nothing could turn one into the other: `swap` demands a
CONTRACT ADDRESS for `token_in` and has no native sentinel, and
`providers/routes/lifi.py` refuses any quote carrying native value. So the agent
concluded it had to IMPORT WETH -- it set an allowance for WETH it did not hold,
then spent twelve hours failing to bridge $3 across three routes to acquire an
asset already sitting in that same wallet in a different form.

The registry had said so all along: "Native ETH in is the cheapest entry and
needs no allowance at all."

These tests pin the two things that make this verb safe to hold: the destination
is the registry's PINNED wrapped native (a caller can never aim it), and the
send is DECLARED to tx_guard as native so the measured outflow is asserted.
"""
import contextlib

import pytest

from core.wallet.tx_guard import Decision
from tools.defi.trade_tool import DefiTradeTool, WrapParams

#: keccak("deposit()")[:4]
DEPOSIT = "0xd0e30db0"


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

    def size_gas(self, tx, sim_gas_used):
        return tx

    def sign_and_send(self, tx):
        self.sent = True
        return "0x" + "ab" * 32

    def await_receipt(self, tx_hash, **kw):
        from core.wallet.broadcast.evm import Receipt
        return Receipt(tx_hash=tx_hash, status="success", block_number=7,
                       gas_used=27000)


def _tool(*, allow=True, captured=None, balances=None):
    gate = _Gate()

    def _guard(intent, tx, **kw):
        if captured is not None:
            captured.append(intent)
        return Decision(allowed=allow, reason="test", lane="autonomous",
                        amount_usd=3.0)

    seq = list(balances or [])

    def _balance_fn(chain, holder, token):
        return seq.pop(0) if seq else None

    return DefiTradeTool(
        wallet=_Wallet(gate), rail_factory=_Rail, guard_fn=_guard,
        price_fn=lambda c, a: 2500.0, fallback_price_fn=lambda c, a: None,
        balance_fn=_balance_fn,
    ), gate


@pytest.mark.asyncio
async def test_it_calls_deposit_on_the_registry_pinned_wrapped_native():
    """A caller supplies an AMOUNT and a CHAIN -- never a destination.

    This is the whole safety argument for the verb: wrapping has no route and no
    counterparty, so the only way to lose money is to send native to the wrong
    contract, and a caller cannot aim it.
    """
    from core.wallet import chains
    _Rail.last = None
    tool, _ = _tool()
    res = await tool.wrap(WrapParams(chain="base", amount=0.0012,
                                     max_spend_usd=5.0, dry_run=True))
    assert res.error is None, res.error
    built = _Rail.last.built
    assert built["to"] == chains.get("base").wrapped_native
    assert built["data"] == DEPOSIT
    assert built["value"] == 1_200_000_000_000_000   # 0.0012e18


@pytest.mark.asyncio
async def test_the_send_is_DECLARED_native_so_the_outflow_is_asserted():
    """`token=None` is what makes tx_guard measure the native delta (039 B1).

    Declaring a token here instead would send tx_guard looking for an ERC-20
    outflow that does not exist, and its `intent.token` branch additionally
    REFUSES an unexpected native balance change -- so the wrap would be refused
    by the very guard meant to bound it.
    """
    captured = []
    tool, _ = _tool(captured=captured)
    await tool.wrap(WrapParams(chain="base", amount=0.0012,
                               max_spend_usd=5.0, dry_run=True))
    assert len(captured) == 1
    intent = captured[0]
    assert intent.token is None
    assert intent.amount_raw == 1_200_000_000_000_000
    assert intent.expected_allowance_grants == ()


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_broadcasts_nothing():
    _Rail.last = None
    tool, _ = _tool()
    res = await tool.wrap(WrapParams(chain="base", amount=0.001,
                                     max_spend_usd=5.0))
    assert "DRY RUN" in res.extracted_content
    assert not _Rail.last.sent


@pytest.mark.asyncio
async def test_a_guard_refusal_broadcasts_nothing():
    _Rail.last = None
    tool, _ = _tool(allow=False)
    res = await tool.wrap(WrapParams(chain="base", amount=0.001,
                                     max_spend_usd=5.0, dry_run=False))
    assert "NOT SENT" in res.extracted_content
    assert not _Rail.last.sent


@pytest.mark.asyncio
async def test_it_reports_the_MEASURED_delta_not_the_requested_one():
    """The claim at the end is "the WETH arrived", so it is measured. Every
    sibling verb on this rail proves its effect rather than restating its input.
    """
    tool, gate = _tool(balances=[0, 1_200_000_000_000_000])
    res = await tool.wrap(WrapParams(chain="base", amount=0.0012,
                                     max_spend_usd=5.0, dry_run=False))
    assert "WRAPPED AND CONFIRMED" in res.extracted_content
    assert "measured: +0.0012" in res.extracted_content
    assert gate.recorded and gate.recorded[0]["action"] == "wrap"


@pytest.mark.asyncio
async def test_an_unreadable_balance_says_so_rather_than_implying_zero():
    """Unlike the bridge, an unread balance is not fatal here -- the receipt
    already proves the atomic call executed. But it must not be rendered as a
    delta of nothing, which would read as "the wrap did nothing"."""
    tool, _ = _tool(balances=[None, None])
    res = await tool.wrap(WrapParams(chain="base", amount=0.0012,
                                     max_spend_usd=5.0, dry_run=False))
    assert "WRAPPED AND CONFIRMED" in res.extracted_content
    assert "unreadable" in res.extracted_content
    assert "measured: +0" not in res.extracted_content


@pytest.mark.asyncio
async def test_a_chain_with_no_evm_wrapped_native_refuses():
    """Solana pins a base58 MINT, not an EVM address. Refusing beats inventing
    a destination -- and beats case-folding base58, which is case-SENSITIVE."""
    tool, _ = _tool()
    res = await tool.wrap(WrapParams(chain="solana", amount=0.1,
                                     max_spend_usd=5.0, dry_run=True))
    # `_unsupported_chain` catches it first, with the better message: it names
    # the SVM-native verb to use instead. Either refusal is correct; what must
    # never happen is a wrap aimed at a base58 mint.
    assert res.error and "read-only here" in res.error
