"""`unwrap` — WETH back into gas.

`wrap` shipped one-way on 2026-09-13. That leaves a real trap: the agent can turn
its gas into WETH to trade and then be unable to pay for a transaction, and the
only ways back were a swap (which needs a route and a counterparty for an asset
that is 1:1 by construction) or nothing at all.

The shape already exists in the guard: a TOKEN outflow with a declared minimum
NATIVE inflow — the 042 assertion. Nothing new is needed there.

⚠️ The minimum is `amount - worst-case fee of this very transaction`, computed
from the built tx rather than from a fixed dust constant: 10**12 wei covers gas
on Robinhood Chain and does not come close on Ethereum, so a constant would be
either too loose or a false refusal depending on the chain.
"""
import pytest

from tools.defi.trade_tool import DefiTradeTool, UnwrapParams

WETH_BASE = "0x4200000000000000000000000000000000000006"


def _text(res):
    return (getattr(res, "extracted_content", None) or "") + (getattr(res, "error", None) or "")


def test_the_withdraw_selector_is_correct():
    from core.wallet import abi
    assert abi.encode_call("withdraw", [{"name": "wad", "type": "uint256"}],
                           [0]).startswith("0x2e1a7d4d")


def test_the_minimum_is_the_amount_less_this_transactions_worst_case_fee():
    from tools.defi.trade_tool import _unwrap_min_native_wei
    amount = 10 ** 18
    # 60k gas at 1 gwei = 6e13 wei.
    assert _unwrap_min_native_wei(amount, {"gas": 60_000, "maxFeePerGas": 10 ** 9}) \
        == amount - 60_000 * 10 ** 9


def test_the_minimum_never_goes_negative_or_zero():
    from tools.defi.trade_tool import _unwrap_min_native_wei
    assert _unwrap_min_native_wei(10, {"gas": 10 ** 9, "maxFeePerGas": 10 ** 9}) >= 1


def test_a_tx_with_no_fee_fields_asserts_the_full_amount():
    from tools.defi.trade_tool import _unwrap_min_native_wei
    assert _unwrap_min_native_wei(10 ** 18, {}) == 10 ** 18


@pytest.mark.asyncio
async def test_a_chain_with_no_pinned_wrapped_native_is_refused():
    tool = DefiTradeTool(name="defi_trade")
    out = _text(await tool.unwrap(UnwrapParams(
        chain="solana", amount=0.1, max_spend_usd=1.0)))
    assert "solana" in out.lower()


@pytest.mark.asyncio
async def test_a_non_positive_amount_is_refused():
    tool = DefiTradeTool(name="defi_trade")
    with pytest.raises(Exception):
        UnwrapParams(chain="base", amount=0, max_spend_usd=1.0)
