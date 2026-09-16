"""Opt-in LIVE check against api.relay.link (037).

Unit tests pin the parsing and the policy against a fixed body; this one pins
that the SHAPE we parse is still the shape Relay actually sends. A provider
changing a field name is exactly the failure that unit tests with a frozen
fixture cannot see, and it would land as a refusal on the money path.

    RUN_LIVE_RELAY_TESTS=1 pytest tests/unit/tools/defi/test_bridge_live_relay.py
"""
import os

import pytest

_OPT_IN = os.getenv("RUN_LIVE_RELAY_TESTS", "").lower() in ("1", "true", "yes")

pytestmark = pytest.mark.skipif(
    not _OPT_IN,
    reason="Live network test; set RUN_LIVE_RELAY_TESTS=1 to run.")

# Public addresses only — no secret is needed to QUOTE.
EVM = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
SOL = "BrsPwATRZcb2PWsEZba9Bh1mwcxU6M7R64nPgRneCpmL"


def test_the_live_route_still_parses_and_yields_a_positive_floor():
    from tools.defi.providers.relay_bridge import (NATIVE_EVM, NATIVE_SVM,
                                                   RelayBridgeProvider,
                                                   SOLANA_CHAIN_ID)
    q = RelayBridgeProvider().quote(
        origin_chain_id=SOLANA_CHAIN_ID, dest_chain_id=4663,
        origin_currency=NATIVE_SVM, dest_currency=NATIVE_EVM,
        amount_in_raw=500_000_000, sender=SOL, recipient=EVM)
    assert q.request_id
    assert q.amount_out_raw > 0
    assert 0 < q.min_out_raw < q.amount_out_raw, "the arrival floor must bite"
    assert q.svm_origin is True
    assert q.recipient == EVM


def test_the_live_solana_order_needs_exactly_our_signature_and_fits_a_packet():
    """An order requiring a second signer is not one we can fulfil; an oversized
    one is refused rather than compressed through a lookup table."""
    from solders.hash import Hash

    from core.wallet.relay_svm import (MAX_TX_BYTES, build_transaction,
                                       signer_accounts)
    from tools.defi.providers.relay_bridge import (NATIVE_EVM, NATIVE_SVM,
                                                   RelayBridgeProvider,
                                                   SOLANA_CHAIN_ID)
    q = RelayBridgeProvider().quote(
        origin_chain_id=SOLANA_CHAIN_ID, dest_chain_id=4663,
        origin_currency=NATIVE_SVM, dest_currency=NATIVE_EVM,
        amount_in_raw=500_000_000, sender=SOL, recipient=EVM)
    instructions = q.tx_data["instructions"]
    assert signer_accounts(instructions) == {SOL}
    tx = build_transaction(instructions=instructions, payer=SOL,
                           recent_blockhash=str(Hash.default()))
    assert 0 < len(bytes(tx)) <= MAX_TX_BYTES


# --------------------------------------------------------------------------
# 039: the EVM origin, and an ERC-20 destination
# --------------------------------------------------------------------------

def test_the_live_evm_origin_order_matches_what_we_asked_for():
    """Base -> Robinhood, the leg the owner could not run on 2026-09-12.

    This is the whole pre-broadcast chain against the real API: the parser, the
    signable item's own assertion, and phase 1. Everything but the signature.
    """
    from core.wallet import bridge_guard
    from tools.defi.bridge_evm_leg import assert_order_matches_request
    from tools.defi.providers.relay_bridge import NATIVE_EVM, RelayBridgeProvider

    amount = 36 * 10 ** 15          # 0.036 ETH
    q = RelayBridgeProvider().quote(
        origin_chain_id=8453, dest_chain_id=4663,
        origin_currency=NATIVE_EVM, dest_currency=NATIVE_EVM,
        amount_in_raw=amount, sender=EVM, recipient=EVM)
    assert q.svm_origin is False, "an EVM origin must NOT take the Solana path"
    assert 0 < q.min_out_raw < q.amount_out_raw
    assert q.amount_in_usd is not None, "an unvalued outflow refuses at the verb"

    # The signable item is a different object from the quote details, and it is
    # the one we actually sign.
    assert assert_order_matches_request(
        q.tx_data, origin_chain_id=8453, amount_in_raw=amount) is None
    assert int(q.tx_data["value"]) == amount

    verdict = bridge_guard.assert_phase1(
        quote=q, expected_recipient=EVM, declared_amount_raw=amount,
        dest_chain_name="robinhood")
    assert verdict.ok, verdict.reason


def test_a_live_erc20_destination_quotes_and_selects_the_token_reader():
    """'in WETH'. The destination asset must come back as the PINNED address, and
    phase 2 must pick the ERC-20 reader — measuring the native balance for a token
    arrival would watch a number that cannot move."""
    from core.wallet import bridge_guard, chains
    from tools.defi.bridge_verb import resolve_dest_currency
    from tools.defi.providers.relay_bridge import NATIVE_EVM, RelayBridgeProvider

    weth, label = resolve_dest_currency("weth", "robinhood")
    assert weth == chains.get("robinhood").wrapped_native and label == "weth"

    amount = 36 * 10 ** 15
    q = RelayBridgeProvider().quote(
        origin_chain_id=8453, dest_chain_id=4663,
        origin_currency=NATIVE_EVM, dest_currency=weth,
        amount_in_raw=amount, sender=EVM, recipient=EVM)
    assert q.symbol_out == "WETH"
    assert q.currency_out.lower() == weth.lower()
    assert 0 < q.min_out_raw < q.amount_out_raw

    reader = bridge_guard.arrival_reader(q.currency_out)
    assert reader is not bridge_guard.native_balance_raw, (
        "an ERC-20 arrival must not be measured against the native balance")
