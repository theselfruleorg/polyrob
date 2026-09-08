"""Solana x402 pay-side seam. Phase 4.

**This phase was NOT blocked.** An earlier note (carried from the Solana
research) said Solana x402 depended on the spec shipping an exact-SVM scheme.
The installed SDK (`x402` 2.16.0) already ships one —
`x402/mechanisms/svm/exact/{client,server,facilitator}.py`, with `solana` and
`solana-devnet` as networks. The claim was inherited rather than checked.

What the SDK needs from us is a `ClientSvmSigner`, and that protocol asks for
the raw `Keypair`. `SolanaSigner` deliberately does not hand one out — the key
never crosses its boundary. So this adapter is the seam: it satisfies the SDK
IN-PROCESS and leaves the signer's perimeter intact everywhere else.

That is not a new concession. `LocalEoaSigner.account` already exists for
exactly this reason (the Hyperliquid SDK) with the same caveat, and the same one
applies here: **in-process use only — never returned from a tool action, never
logged, never serialized.**

The fee-payer refusal is preserved rather than bypassed. An x402 SVM payment is
fee-paid by the FACILITATOR, so the transaction the SDK asks us to sign names
the facilitator as payer and us as the token authority — which means the naive
"payer must be us" check would refuse every legitimate payment. `sign_transaction`
here therefore delegates to the underlying signer only when we ARE the payer,
and otherwise signs as a non-payer authority through an explicit, narrower path
that still refuses a transaction we do not appear in at all.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                                       # pragma: no cover
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction

#: Our wallet network name -> the x402 network identifier.
_X402_NETWORKS = {"mainnet": "solana", "testnet": "solana-devnet"}

#: Mirrors x402.mechanisms.svm.constants. Pinned here so a mint is never taken
#: from a caller and never silently defaults to mainnet on a testnet run.
_USDC_MINTS = {
    "mainnet": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "testnet": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
}


def x402_network(network: str) -> str:
    """The x402 network id for our wallet network name."""
    key = (network or "testnet").strip().lower()
    if key not in _X402_NETWORKS:
        raise ValueError(
            f"unknown wallet network {network!r} — expected one of "
            f"{sorted(_X402_NETWORKS)}. Refusing rather than defaulting: "
            f"defaulting to mainnet on a testnet run spends real money.")
    return _X402_NETWORKS[key]


def usdc_mint(network: str) -> str:
    """The USDC mint for *network*. Devnet USDC is a DIFFERENT mint, and paying
    the mainnet one on devnet (or the reverse) is a silent misdirection."""
    key = (network or "testnet").strip().lower()
    if key not in _USDC_MINTS:
        raise ValueError(f"unknown wallet network {network!r}")
    return _USDC_MINTS[key]


class SolanaX402Signer:
    """Adapts ``SolanaSigner`` to the SDK's ``ClientSvmSigner`` protocol.

    ⚠️ Holds a reference to the underlying keypair because the SDK's protocol
    requires it. In-process use only — never return this object (or its
    ``keypair``) from a tool action, never log it, never serialize it.
    """

    def __init__(self, signer):
        self._signer = signer

    @property
    def address(self) -> str:
        return self._signer.address

    @property
    def keypair(self) -> "Keypair":
        """The raw keypair the SDK protocol demands. See the class caveat."""
        # Reaches the name-mangled attribute deliberately and in ONE place, so
        # the exception to the perimeter is greppable rather than diffuse.
        return getattr(self._signer, "_SolanaSigner__keypair")

    def sign_transaction(self, tx: "VersionedTransaction") -> "VersionedTransaction":
        """Sign as the token authority.

        An x402 SVM payment is fee-paid by the FACILITATOR, so the transaction
        names the facilitator as payer and us merely as the token authority.
        The underlying signer's "payer must be us" rule would refuse every
        legitimate payment, so it is applied only when we ARE the payer; in the
        facilitator case we still refuse a transaction our address does not
        appear in at all, which is the property that actually matters — we must
        never sign for an account set we are not part of.
        """
        keys = [str(k) for k in tx.message.account_keys]
        if not keys:
            raise ValueError("refusing to sign a transaction with no accounts")
        if self.address not in keys:
            raise ValueError(
                f"refusing to sign: this transaction does not involve "
                f"{self.address} at all. Signing for an account set we are not "
                f"part of is never correct.")
        if keys[0] == self.address:
            # We are the fee payer, so the strict rule applies and the base
            # signer owns it.
            return self._signer.sign_transaction(tx)
        # PARTIAL signing. `VersionedTransaction(message, [our_keypair])` raises
        # "not enough signers" because the facilitator's slot is still empty,
        # and `solders`' VersionedTransaction is IMMUTABLE in 0.28/0.29 — there
        # is no `.sign()` to fill one slot (the SDK's own reference signer
        # targets a version that has it). So the signature is placed by hand:
        # sign the serialised message, drop it in OUR slot, and leave every
        # other slot exactly as it was for the facilitator to fill.
        from solders.transaction import VersionedTransaction
        index = keys.index(self.address)
        required = tx.message.header.num_required_signatures
        if index >= required:
            raise ValueError(
                f"refusing to sign: {self.address} appears in this transaction "
                f"but not as a required signer (slot {index} of {required}). "
                f"Signing into a slot that is not ours corrupts the payload.")
        signatures = list(tx.signatures)
        signatures[index] = self.keypair.sign_message(bytes(tx.message))
        return VersionedTransaction.populate(tx.message, signatures)

    def __repr__(self) -> str:                          # never leak the key
        return f"<SolanaX402Signer address={self.address}>"
