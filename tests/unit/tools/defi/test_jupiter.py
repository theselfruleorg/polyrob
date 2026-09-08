"""Jupiter as Solana's route provider. Phase 3.

Settles hard mismatch #2 from the Solana research by PRECEDENT rather than
argument: LI.FI's aggregator-authored calldata was accepted on EVM under
simulation + delta assertion + caps, and a live trade proved the discipline
holds. Jupiter is the same bargain on a different chain — it decides the PATH,
and what we accept as an OUTCOME stays ours.

The Solana-specific catch is that Jupiter returns a whole SIGNED-SHAPED
transaction, not calldata. So there is no `spender` to pin (mismatch #3:
`approve -> swap -> revoke` is meaningless here) and the fee payer becomes the
thing that must be checked instead.
"""
import base64

import pytest

from tools.defi.providers import jupiter

ME = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"


def _quote(out="10203299", min_out="10101266", slippage=150, mints=(USDC, WSOL)):
    return {"inputMint": mints[0], "outputMint": mints[1], "inAmount": "1000000",
            "outAmount": out, "otherAmountThreshold": min_out,
            "slippageBps": slippage, "routePlan": [{"swapInfo": {"label": "Raydium"}},
                                                   {"swapInfo": {"label": "Orca"}}]}


def test_a_quote_carries_the_output_and_the_floor():
    q = jupiter.parse_quote(_quote(), chain="solana")
    assert q.amount_out_raw == 10_203_299
    assert q.amount_out_min_raw == 10_101_266


def test_the_venue_names_the_actual_route():
    """So the agent can see WHICH pools it went through, the way the EVM side
    names lifi:kyberswap."""
    q = jupiter.parse_quote(_quote(), chain="solana")
    assert "jupiter" in q.venue
    assert "Raydium" in q.venue or "Orca" in q.venue


def test_a_zero_output_quote_is_refused():
    assert jupiter.parse_quote(_quote(out="0"), chain="solana") is None


def test_a_quote_with_no_floor_is_refused():
    """An unverifiable floor is not a floor — same rule as the EVM seam."""
    assert jupiter.parse_quote(_quote(min_out=None), chain="solana") is None


def test_a_junk_payload_is_none_not_an_exception():
    for bad in (None, {}, {"outAmount": "abc"}, []):
        assert jupiter.parse_quote(bad, chain="solana") is None


def test_a_floor_looser_than_our_slippage_is_refused():
    """Jupiter bakes its own slippage into the transaction it builds, exactly
    like LI.FI. We cannot rewrite it, so a loose floor is a REFUSAL."""
    loose = _quote(out="1000000", min_out="1")
    assert jupiter.verified_floor(loose, slippage_bps=150) is None


def test_a_floor_that_clears_our_bound_is_kept():
    ok = _quote(out="1000000", min_out="990000")
    assert jupiter.verified_floor(ok, slippage_bps=150) == 990_000


# -- the swap transaction ----------------------------------------------------

def test_the_swap_transaction_is_decoded_from_base64():
    raw = b"\x01\x02\x03"
    body = {"swapTransaction": base64.b64encode(raw).decode()}
    assert jupiter.decode_swap_transaction(body) == raw


def test_a_missing_swap_transaction_is_none():
    for bad in ({}, {"swapTransaction": ""}, None, {"swapTransaction": "!!not-b64!!"}):
        assert jupiter.decode_swap_transaction(bad) is None


def test_the_route_is_same_chain_only():
    """Jupiter is Solana-only, but the guard belongs here anyway: a provider
    asked about another chain must decline rather than answer for Solana."""
    assert jupiter.supports("solana") is True
    for other in ("base", "ethereum", "robinhood"):
        assert jupiter.supports(other) is False
