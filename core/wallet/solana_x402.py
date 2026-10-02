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


_TOKEN_PROGRAMS = frozenset({
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",      # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",      # Token-2022
})
_ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
_COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
#: The SDK's `exact` SVM client always appends a Memo (a nonce) with no accounts.
_MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
_TRANSFER_CHECKED = 12


def _ata(owner: str, mint: str, token_program: str) -> str:
    from solders.pubkey import Pubkey
    ata, _bump = Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)),
         bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(_ATA_PROGRAM))
    return str(ata)


class SolanaX402Signer:
    """Adapts ``SolanaSigner`` to the SDK's ``ClientSvmSigner`` protocol.

    ⚠️ Holds a reference to the underlying keypair because the SDK's protocol
    requires it. In-process use only — never return this object (or its
    ``keypair``) from a tool action, never log it, never serialize it.

    ⚠️ CR-L28: it signs ONE shape only — the payment it was PINNED to (mint,
    payTo, raw amount): exactly one SPL ``TransferChecked`` of that mint, from
    our token account to payTo's token account, for that amount, with us as the
    sole authority, plus ComputeBudget and the SDK's account-less Memo. An
    unpinned signer signs nothing. Being "a required signer" is not consent.
    """

    def __init__(self, signer, *, mint: str = None, pay_to: str = None,
                 amount: int = None):
        self._signer = signer
        self._pin = None
        if mint is not None or pay_to is not None or amount is not None:
            self.pin(mint=mint, pay_to=pay_to, amount=amount)

    def pin(self, *, mint: str, pay_to: str, amount: int) -> None:
        """Bind the ONE payment this signer may authorize."""
        if not mint or not pay_to or amount is None or int(amount) <= 0:
            raise ValueError("a pin needs a mint, a payTo and a positive raw amount")
        self._pin = (str(mint), str(pay_to), int(amount))

    def _refuse_unless_pinned_transfer(self, tx) -> None:
        if self._pin is None:
            raise ValueError(
                "refusing to sign: no payment is pinned (mint, payTo, amount). "
                "This signer only authorizes the one transfer it was bound to.")
        mint, pay_to, amount = self._pin
        message = tx.message
        if list(getattr(message, "address_table_lookups", None) or []):
            raise ValueError("refusing to sign: address-table lookups hide "
                             "accounts this check cannot read")
        keys = [str(k) for k in message.account_keys]
        transfers = 0
        for ix in message.instructions:
            program = keys[ix.program_id_index]
            accounts = [keys[i] for i in bytes(ix.accounts)]
            data = bytes(ix.data)
            if program == _COMPUTE_BUDGET_PROGRAM and not accounts:
                continue
            if program == _MEMO_PROGRAM and not accounts:
                continue
            if program not in _TOKEN_PROGRAMS:
                raise ValueError(f"refusing to sign: unexpected program {program}")
            if len(data) != 10 or data[0] != _TRANSFER_CHECKED or len(accounts) != 4:
                raise ValueError("refusing to sign: the token instruction is not a "
                                 "plain TransferChecked")
            src, ix_mint, dest, authority = accounts
            ix_amount = int.from_bytes(data[1:9], "little")
            if (ix_mint != mint or authority != self.address
                    or src != _ata(self.address, mint, program)
                    or dest != _ata(pay_to, mint, program) or ix_amount != amount):
                raise ValueError(
                    "refusing to sign: the transfer does not match the pinned "
                    "payment (mint, payTo, amount, authority)")
            transfers += 1
        if transfers != 1:
            raise ValueError(f"refusing to sign: expected exactly one transfer, "
                             f"found {transfers}")

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
        self._refuse_unless_pinned_transfer(tx)                 # CR-L28
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
