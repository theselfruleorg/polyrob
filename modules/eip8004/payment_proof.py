"""ERC-8004 payment-backed reputation: map a settled x402 invoice into the
EXISTING ``ProofOfPayment`` model (Task 15, Phase 4).

8004 is the TRUST layer, not a payment rail — x402 answers "how do I pay",
8004 answers "who is this agent, can I trust it." This module is the pure
compose seam between them: a settled x402 invoice dict in, a ``ProofOfPayment``
(``modules/eip8004/models.py:102``) out. No I/O, no settlement-status check —
that lives in ``ReputationManager._verify_payment_proof``
(``modules/eip8004/reputation.py``), which queries x402 directly so a proof
can be re-verified independently of how it was originally built.
"""
from typing import Any, Dict

from .models import ProofOfPayment

# x402's `chain` field is a NETWORK NAME (`base`, `base-sepolia`, ...) — see
# modules/x402/x402_integration.py::get_x402_config()['network'] — while
# ERC-8004's ProofOfPayment.chainId is conventionally a numeric EVM chain id,
# matching EIP8004Config.chain_id / the `eip155:<chainId>:<address>` CAIP-10
# format this module already uses (registration.py, reputation.py). Known
# x402 networks map to their numeric id; an unrecognized name passes through
# unchanged rather than raising — this is a display/reference field, not a
# verification input (verification is anchored on txHash + settlement status,
# not chainId — see reputation.py::_verify_payment_proof).
_NETWORK_TO_CHAIN_ID = {
    "base": "8453",
    "base-sepolia": "84532",
}


def proof_from_settled_invoice(invoice: Dict[str, Any]) -> ProofOfPayment:
    """Build a ``ProofOfPayment`` from a settled x402 invoice dict.

    Recognized keys (all optional except the transaction hash — see below):
    ``tx_hash`` / ``transaction_hash`` (the on-chain settlement tx),
    ``chain`` (x402 network name), ``recipient`` / ``to_address`` (treasury),
    ``payer_address`` / ``from_address`` / ``payer_hint`` (the payer's wallet,
    when known — x402 invoices do not always record one, so this is
    best-effort and defaults to ``""`` rather than raising).

    Raises ``ValueError`` when no transaction hash is present — a
    "ProofOfPayment" with no on-chain transaction proves nothing (mirrors
    this module's existing fail-closed signing philosophy — see
    ``ReputationManager.create_feedback_auth``'s "never emit a placeholder"
    rule). A settlement with no tx hash (e.g. a manually attested
    ``settled_no_tx`` invoice) simply has no feedback-authorization offer —
    callers should check for a tx hash BEFORE calling this, not call it and
    catch the error as control flow.
    """
    tx_hash = str(invoice.get("tx_hash") or invoice.get("transaction_hash") or "").strip()
    if not tx_hash:
        raise ValueError(
            "cannot build a ProofOfPayment without a settlement transaction hash"
        )
    chain = str(invoice.get("chain") or "").strip()
    chain_id = _NETWORK_TO_CHAIN_ID.get(chain.lower(), chain)
    to_address = str(invoice.get("recipient") or invoice.get("to_address") or "").strip()
    from_address = str(
        invoice.get("payer_address")
        or invoice.get("from_address")
        or invoice.get("payer_hint")
        or ""
    ).strip()
    return ProofOfPayment(
        fromAddress=from_address,
        toAddress=to_address,
        chainId=chain_id,
        txHash=tx_hash,
    )
