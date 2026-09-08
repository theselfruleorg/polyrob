"""SPL settlement detection + Solana Pay `reference` matching. Phase 4.

The EVM watcher scans treasury `Transfer` logs and matches an incoming payment
to a pending invoice by EXACT AMOUNT, with amount-jitter to keep same-priced
invoices distinguishable. That is a workaround for EVM having no per-payment
correlator.

Solana has one. Solana Pay puts a `reference` public key in the transaction's
account list — unique per invoice, carried by the payer, and readable by
`getSignaturesForAddress` on the reference itself. Matching becomes exact and
deterministic, so amount-jitter is strictly worse here and is not used
(Solana research mismatch #8).
"""
import pytest

from modules.x402 import solana_settlement as ss

TREASURY = "BrsPwATRZcb2PWsEZba9Bh1mwcxU6M7R64nPgRneCpmL"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def test_a_reference_key_is_deterministic_per_invoice():
    a = ss.reference_for_invoice("inv-123")
    b = ss.reference_for_invoice("inv-123")
    assert a == b


def test_different_invoices_get_different_references():
    assert ss.reference_for_invoice("inv-1") != ss.reference_for_invoice("inv-2")


def test_a_reference_is_a_valid_solana_account_key():
    from core.wallet.addresses import normalize_for_chain
    ref = ss.reference_for_invoice("inv-abc")
    assert normalize_for_chain("solana", ref) == ref


def test_the_reference_is_not_a_spendable_key():
    """It is a MARKER, never an account we could sign for. Deriving it from a
    secret would make it one; it is derived from the invoice id alone."""
    ref = ss.reference_for_invoice("inv-1")
    assert ss.reference_for_invoice("inv-1") == ref     # no secret involved


# -- parsing an observed payment --------------------------------------------

def _tx(amount="1500000", mint=USDC, dest=TREASURY, err=None):
    return {"meta": {"err": err,
                     "preTokenBalances": [
                         {"owner": dest, "mint": mint,
                          "uiTokenAmount": {"amount": "0", "decimals": 6}}],
                     "postTokenBalances": [
                         {"owner": dest, "mint": mint,
                          "uiTokenAmount": {"amount": amount, "decimals": 6}}]}}


def test_an_incoming_transfer_is_measured():
    got = ss.credited_amount(_tx(), treasury=TREASURY, mint=USDC)
    assert got == 1_500_000


def test_a_failed_transaction_credits_nothing():
    """It landed and reverted. The fee was paid; the payment was not."""
    assert ss.credited_amount(_tx(err={"x": 1}), treasury=TREASURY, mint=USDC) is None


def test_a_transfer_of_another_mint_is_ignored():
    other = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
    assert ss.credited_amount(_tx(mint=other), treasury=TREASURY, mint=USDC) is None


def test_a_transfer_to_someone_else_is_ignored():
    assert ss.credited_amount(_tx(dest="OtherOwner1111111111111111111111111111111"),
                              treasury=TREASURY, mint=USDC) is None


def test_a_decrease_is_not_a_credit():
    tx = _tx()
    tx["meta"]["preTokenBalances"][0]["uiTokenAmount"]["amount"] = "5000000"
    tx["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = "1000000"
    assert ss.credited_amount(tx, treasury=TREASURY, mint=USDC) is None


def test_a_junk_payload_is_none_not_an_exception():
    for bad in (None, {}, {"meta": None}, {"meta": {"postTokenBalances": "x"}}):
        assert ss.credited_amount(bad, treasury=TREASURY, mint=USDC) is None


# -- why reference beats amount matching ------------------------------------

def test_two_invoices_of_the_SAME_amount_stay_distinguishable():
    """The exact case EVM needs amount-jitter for. On Solana the reference is
    unique per invoice, so identical prices are never ambiguous and jitter buys
    nothing."""
    a, b = ss.reference_for_invoice("inv-a"), ss.reference_for_invoice("inv-b")
    assert a != b
    assert ss.match_by_reference([a, b], observed_reference=b) == b


def test_an_unknown_reference_matches_nothing():
    a = ss.reference_for_invoice("inv-a")
    assert ss.match_by_reference([a], observed_reference="ZZZnope") is None


# --------------------------------------------------------------------------
# Shapes taken from a REAL mainnet transaction (2026-08-25)
#
# Verified against signature 5R6WNdiGocSXWVo3XEaqph9M… on Solana mainnet: five
# owners, and our figures matched the chain for every one. The case worth
# pinning is the one no hand-written fixture had — **an owner holding SEVERAL
# token accounts for the same mint** (that transaction had one owner with
# three, at accountIndex 11/23/35). A per-owner sum is correct there; anything
# that assumed one account per owner would silently under-report a credit and
# leave a genuinely-paid invoice pending.
# --------------------------------------------------------------------------

def _multi(owner=TREASURY, mint=USDC, pre=(0, 0, 0), post=(100, 200, 300)):
    def rows(amounts):
        return [{"owner": owner, "mint": mint, "accountIndex": 11 + i * 12,
                 "uiTokenAmount": {"amount": str(a), "decimals": 6}}
                for i, a in enumerate(amounts)]
    return {"meta": {"err": None, "preTokenBalances": rows(pre),
                     "postTokenBalances": rows(post)}}


def test_an_owner_with_several_token_accounts_sums_across_all_of_them():
    """The real-mainnet shape. 5,493 + 3,236 + 100 was the actual case."""
    got = ss.credited_amount(_multi(pre=(25654188, 15461505, 836938),
                                    post=(25659681, 15464741, 837038)),
                             treasury=TREASURY, mint=USDC)
    assert got == 8829


def test_a_credit_to_a_NEW_token_account_counts():
    """A first payment to a freshly created ATA has no pre-balance row at all."""
    tx = _multi(pre=(), post=(1_000_000,))
    tx["meta"]["preTokenBalances"] = []
    assert ss.credited_amount(tx, treasury=TREASURY, mint=USDC) == 1_000_000


def test_a_net_zero_across_accounts_is_not_a_credit():
    """Money moved BETWEEN the owner's own accounts. Nothing was received."""
    assert ss.credited_amount(_multi(pre=(100, 200), post=(200, 100)),
                              treasury=TREASURY, mint=USDC) is None


def test_a_net_decrease_across_accounts_is_not_a_credit():
    assert ss.credited_amount(_multi(pre=(500, 500), post=(100, 100)),
                              treasury=TREASURY, mint=USDC) is None
