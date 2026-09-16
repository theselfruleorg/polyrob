"""A SOL-input swap must survive its own network fees.

When the wallet spends NATIVE SOL, the fee and the principal are the SAME asset.
The declared-amount bound folded native SOL into the outflow (correct — the fold
is how a native sell is observed at all) and then required

    outflow <= amount_in + max(1, amount_in // 1000)

with only a 0.1% dust tolerance. But the folded `native_delta` also carries the
transaction fee and the rent for the output token account, so the measured
outflow is *always* `amount_in + fees + rent`. Prod measured that constant at
3,670,479 lamports. For 0.1% of the declared amount to cover it you would have
to declare ~3,670 SOL (~$378k) in one swap — so the bound was unsatisfiable at
every size the treasury could ever trade, and refused 100% of SOL-input swaps.

Live: 2026-09-09, goal e9c585d9. The agent screened nine candidates clean, then
reported "guard refuses every SOL-input swap ... unsatisfiable at any size" and
blocked. That was an accurate report of a real defect, and it is why a treasury
whose only asset was SOL never opened a position.

The non-SOL branch never had this problem: it reads the outflow from
`token_deltas[token_in]` (pure token movement) and classifies native movement
separately through `is_plausible_rent`. The SOL branch cannot separate them by
ASSET, so it must separate them by ARITHMETIC — classifying the excess over the
declared amount exactly as the other branch classifies native movement.
"""
import types

import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from core.wallet.solana_simulation import (MAX_PLAUSIBLE_RENT_LAMPORTS,
                                           SolanaDeltas)
from tests.unit.tools.defi.test_solana_swap_observation import (
    USDC, WSOL, _sol_params, _sol_tool)

#: Measured on prod, 2026-09-09: Jupiter fee + ATA rent on a SOL-input swap.
PROD_FEE_AND_RENT = 3_670_479


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    for var in ("DEFI_AUTONOMOUS_TURN_TRADING", "DEFI_MONITOR_EXITS",
                "DEFI_SOLANA_RPC"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
async def test_a_sol_input_swap_is_not_refused_for_its_own_fees():
    """The exact prod shape: 0.93 SOL declared, fees+rent on top."""
    deltas = SolanaDeltas(
        ok=True,
        native_delta=-(930_000_000 + PROD_FEE_AND_RENT),
        token_deltas={USDC: 186_000_000})
    res = await _sol_tool(deltas).solana_swap(_sol_params(amount=0.93))
    assert res.error is None, (
        f"a SOL swap refused for its own network fees: {res.error}")


@pytest.mark.asyncio
async def test_a_small_sol_ticket_is_not_refused_for_its_own_fees():
    """The size that actually matters here.

    The treasury trades $1-5 tickets. At 0.01 SOL the 0.1% tolerance is 10,000
    lamports against ~3.7M of fees, so this is the case that was most hopelessly
    refused — and the one the sizing ladder depends on.
    """
    deltas = SolanaDeltas(
        ok=True,
        native_delta=-(10_000_000 + PROD_FEE_AND_RENT),
        token_deltas={USDC: 2_000_000})
    res = await _sol_tool(deltas).solana_swap(_sol_params(amount=0.01))
    assert res.error is None, (
        f"a small SOL ticket refused for its own network fees: {res.error}")


@pytest.mark.asyncio
async def test_an_excess_beyond_plausible_fees_still_refuses():
    """The bound must still bind. Anything past a plausible rent+fee allowance
    is the drain this check exists to catch, and must refuse as before."""
    deltas = SolanaDeltas(
        ok=True,
        native_delta=-(930_000_000 + MAX_PLAUSIBLE_RENT_LAMPORTS + 1_000_000),
        token_deltas={USDC: 186_000_000})
    res = await _sol_tool(deltas).solana_swap(_sol_params(amount=0.93))
    assert res.error, "an outflow past plausible fees must still refuse"
    assert "declared" in res.error.lower()


@pytest.mark.asyncio
async def test_a_gross_overspend_still_refuses():
    """The pre-existing guarantee, unchanged: declaring 0.93 SOL while 2 SOL
    leaves is a drain, not a fee, and the fee allowance must not launder it."""
    deltas = SolanaDeltas(ok=True,
                          native_delta=-2_000_000_000,
                          token_deltas={USDC: 400_000_000})
    res = await _sol_tool(deltas).solana_swap(_sol_params(amount=0.93))
    assert res.error, "an outflow larger than declared must refuse"
    assert "declared" in res.error.lower()


@pytest.mark.asyncio
async def test_a_non_sol_swap_gets_no_fee_allowance():
    """The allowance is scoped to the SOL-input case ONLY.

    For an SPL-token input the outflow is read from `token_deltas`, which never
    contains a fee — so any excess there is a genuine over-spend and must refuse
    with the original 0.1% tolerance. Widening it for every token would trade a
    false refusal for a real hole.
    """
    MEME = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
    deltas = SolanaDeltas(
        ok=True,
        native_delta=-5_000,                       # ordinary fee, classified
        token_deltas={MEME: -(1_000_000 + 500_000), USDC: 2_000_000})
    tool = _sol_tool(deltas, solana_decimals_fn=lambda m: 9 if m == WSOL else 6)
    res = await tool.solana_swap(
        _sol_params(amount=1.0, token_in=MEME, token_out=USDC))
    assert res.error, "an SPL over-spend must still refuse"
    assert "declared" in res.error.lower()
