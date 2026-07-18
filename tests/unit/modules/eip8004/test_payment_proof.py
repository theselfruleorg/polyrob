"""Task 15 (Phase 4): map a settled x402 invoice -> the EXISTING ProofOfPayment
model (modules/eip8004/models.py:102). Pure mapping, no I/O — settlement-status
verification lives in ReputationManager (see test_reputation_payment_proof.py).
"""
import pytest

from modules.eip8004.models import ProofOfPayment
from modules.eip8004.payment_proof import proof_from_settled_invoice


def test_maps_settled_invoice_to_proof_of_payment():
    invoice = {
        "request_id": "inv_abc123",
        "tx_hash": "0xdeadbeef",
        "amount_usd": 4.0,
        "chain": "base",
        "recipient": "0xTREASURY",
        "payer_address": "0xPAYER",
    }
    proof = proof_from_settled_invoice(invoice)
    assert isinstance(proof, ProofOfPayment)
    assert proof.txHash == "0xdeadbeef"
    assert proof.toAddress == "0xTREASURY"
    assert proof.fromAddress == "0xPAYER"
    # x402's network NAME ("base") maps to the numeric EVM chain id ERC-8004
    # elsewhere in this module uses (EIP8004Config.chain_id, eip155:8453:...).
    assert proof.chainId == "8453"


def test_accepts_transaction_hash_alias_key():
    """The settled-invoice dict shape from settled_unnotified_invoices() uses
    `transaction_hash`, not `tx_hash` — both must work."""
    invoice = {
        "request_id": "inv_x",
        "transaction_hash": "0xfeed",
        "chain": "base-sepolia",
        "recipient": "0xTREASURY",
    }
    proof = proof_from_settled_invoice(invoice)
    assert proof.txHash == "0xfeed"
    assert proof.chainId == "84532"


def test_unknown_chain_name_passes_through_unchanged():
    invoice = {"tx_hash": "0xabc", "chain": "polygon", "recipient": "0xR"}
    proof = proof_from_settled_invoice(invoice)
    assert proof.chainId == "polygon"


def test_missing_payer_address_defaults_to_empty_string():
    """`payer_address if known` — unknown is honest empty string, not a crash."""
    invoice = {"tx_hash": "0xabc", "chain": "base", "recipient": "0xR"}
    proof = proof_from_settled_invoice(invoice)
    assert proof.fromAddress == ""


def test_missing_tx_hash_raises():
    """A 'proof of payment' with no transaction hash proves nothing — fail
    closed rather than emit a hollow ProofOfPayment (mirrors this module's
    existing fail-closed signing philosophy in reputation.py)."""
    with pytest.raises(ValueError):
        proof_from_settled_invoice({"chain": "base", "recipient": "0xR"})


def test_missing_tx_hash_empty_string_raises():
    with pytest.raises(ValueError):
        proof_from_settled_invoice({"tx_hash": "", "chain": "base"})
