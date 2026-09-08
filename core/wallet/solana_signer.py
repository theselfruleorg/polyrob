"""Solana keys and signing. Phase 2 of the Solana plan — no money moves here.

The perimeter rule is the same one ``core/wallet/signer.py`` states for EVM: the
raw key NEVER crosses this boundary outward. Only an address, a signature, or a
signed transaction comes back.

**Derivation is Phantom-compatible** (`m/44'/501'/<account>'/0'`, SLIP-0010 over
ed25519) so the owner can open this account in an ordinary Solana wallet with the
same seed phrase they already hold. That is a deliberate choice from the Solana
research (hard mismatch #7): wallet-import parity beats a POLYROB-internal scheme
because it means the owner can always recover funds without us.

SLIP-0010 is implemented here rather than pulled in: it is ~15 lines for the
hardened-only ed25519 case, and every path we derive is hardened, so the public-
key-derivation half of the spec (the part that is genuinely fiddly) is not needed.

⚠️ **Additive only.** ``derivation.py``'s EVM branch is untouched. A funded
wallet's Ethereum address must not move because a Solana branch appeared — the
addresses-never-change invariant is the one thing that cannot be walked back.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:                                   # pragma: no cover
    from solders.keypair import Keypair
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction

#: SLIP-0010's fixed key for the ed25519 curve.
_ED25519_SEED_KEY = b"ed25519 seed"

#: BIP-44 coin type for Solana. Hardened, like every level below it.
SOLANA_COIN_TYPE = 501

_HARDENED = 0x80000000


def solana_derivation_path(account: int = 0) -> str:
    """The path we derive, in the notation a wallet UI shows.

    Phantom's default. Solflare and Backpack use the same for account 0, so an
    exported seed opens in all three.
    """
    return f"m/44'/{SOLANA_COIN_TYPE}'/{int(account)}'/0'"


def _master_key(seed: bytes) -> tuple[bytes, bytes]:
    digest = hmac.new(_ED25519_SEED_KEY, seed, hashlib.sha512).digest()
    return digest[:32], digest[32:]


def _derive_child(key: bytes, chain_code: bytes, index: int) -> tuple[bytes, bytes]:
    """One HARDENED SLIP-0010 step. ed25519 has no non-hardened derivation, so
    an unhardened index is a caller error rather than a supported mode."""
    if not index & _HARDENED:
        raise ValueError(
            f"ed25519 derivation is hardened-only; index {index} is not hardened")
    data = b"\x00" + key + index.to_bytes(4, "big")
    digest = hmac.new(chain_code, data, hashlib.sha512).digest()
    return digest[:32], digest[32:]


def _bip39_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """BIP-39 mnemonic -> 64-byte seed. PBKDF2-HMAC-SHA512, 2048 rounds.

    Implemented directly so a Solana address does not depend on a wallet library
    whose defaults could change under a funded account.
    """
    normalized = " ".join(str(mnemonic).split())
    return hashlib.pbkdf2_hmac(
        "sha512", normalized.encode("utf-8"),
        ("mnemonic" + passphrase).encode("utf-8"), 2048, dklen=64)


def derive_solana_keypair(mnemonic: str, account: int = 0,
                          passphrase: str = "") -> "Keypair":
    """The ``Keypair`` for *account*. Raises if ``solders`` is unavailable."""
    try:
        from solders.keypair import Keypair
    except ImportError as exc:                                   # pragma: no cover
        raise RuntimeError(
            "Solana support needs `solders` — pip install 'polyrob[solana]'"
        ) from exc
    key, chain_code = _master_key(_bip39_seed(mnemonic, passphrase))
    for level in (44 | _HARDENED, SOLANA_COIN_TYPE | _HARDENED,
                  int(account) | _HARDENED, 0 | _HARDENED):
        key, chain_code = _derive_child(key, chain_code, level)
    return Keypair.from_seed(key)


class SolanaSigner:
    """Signing authority for ONE Solana address.

    Mirrors ``LocalEoaSigner``'s contract: ``address`` out, signatures out, the
    key never. There is no ``sign_typed_data`` analogue and no equivalent of the
    EIP-155 ``chainId`` pin — a Solana transaction commits to a recent blockhash
    instead, which is what bounds its replay window.
    """

    def __init__(self, keypair: "Keypair"):
        self.__keypair = keypair

    @classmethod
    def from_mnemonic(cls, mnemonic: str, account: int = 0,
                      passphrase: str = "") -> "SolanaSigner":
        return cls(derive_solana_keypair(mnemonic, account, passphrase))

    @property
    def address(self) -> str:
        return str(self.__keypair.pubkey())

    def sign_message(self, data: bytes) -> "Signature":
        return self.__keypair.sign_message(bytes(data))

    def sign_transaction(self, tx: "VersionedTransaction") -> "VersionedTransaction":
        """Sign *tx*, or raise if it is not ours to sign.

        ⚠️ SIGNING PERIMETER. The fee payer — the message's first required
        signer — must be THIS address. A transaction built by someone else
        (an aggregator payload, a pasted blob) that names a different payer is
        not ours, and signing it blind is exactly how a payload gets a signature
        it should never have had. The EVM side pins ``chainId`` for the same
        class of reason.
        """
        from solders.transaction import VersionedTransaction
        payer = tx.message.account_keys[0]
        if str(payer) != self.address:
            raise ValueError(
                f"refusing to sign: the transaction's fee payer is {payer}, not "
                f"this signer ({self.address}). A transaction whose first "
                f"required signer is someone else is not ours to sign.")
        return VersionedTransaction(tx.message, [self.__keypair])

    def __repr__(self) -> str:                       # never leak the key
        return f"<SolanaSigner address={self.address}>"
