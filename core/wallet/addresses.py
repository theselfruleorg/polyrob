"""Family-dispatched address validation. Solana Phase 0 groundwork (029 §5).

``tokens.normalize_address`` is EIP-55 and is used pervasively. EIP-55 is a
CHECKSUM, and refusing a failed one is the only typo detector Ethereum has built
in — discarding it becomes a fund-loss vector the moment a money verb exists.

Solana is not that. Base58 addresses carry **no checksum at all**, so the same
guarantee is unavailable there and this module must not imply otherwise: a
mistyped Solana address that still decodes to 32 bytes is a valid, different
account, and nothing here can tell you it was a typo. ``typo_protection_note``
exists so a caller can say which world it is in rather than assuming Ethereum's.

Two rules, both learned from ``chains.py``:

* **The family comes from the REGISTRY, never from the string's shape.**
  Sniffing "starts with 0x" would let a typo silently change which chain an
  address is validated against. The caller names the chain; the registry says
  what that chain is; an unknown chain refuses.
* **Case is never folded on base58.** It is case-SENSITIVE, so lowercasing
  destroys the address. (``modules/x402/invoicing.py``'s ``recipient.lower()``
  is the live example of that bug class, noted in the Solana research.)
"""
from __future__ import annotations

from typing import Optional

#: Bitcoin/Solana base58. 0, O, I and l are excluded precisely because they are
#: the characters people confuse when transcribing.
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

#: A Solana account key is a 32-byte ed25519 public key.
_SOLANA_KEY_BYTES = 32


def family_of(chain: str) -> Optional[str]:
    """``"evm"`` / ``"svm"`` for a known chain, else ``None``."""
    from core.wallet import chains
    row = chains.get(chain)
    return None if row is None else row.family


def b58decode(text: str) -> bytes:
    """Decode base58, or raise ``ValueError``. No checksum is verified because
    plain base58 carries none — this only proves the characters are legal and
    says how many bytes they encode."""
    if not isinstance(text, str) or not text:
        raise ValueError("not a base58 string")
    num = 0
    for ch in text:
        digit = _B58_INDEX.get(ch)
        if digit is None:
            raise ValueError(
                f"{ch!r} is not a base58 character — 0, O, I and l are excluded "
                f"from the alphabet on purpose")
        num = num * 58 + digit
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    # Leading '1's are leading zero bytes, and they are significant.
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + body


def normalize_for_chain(chain: str, addr: str) -> str:
    """The canonical form of *addr* on *chain*, or raise ``ValueError``.

    EVM: EIP-55 checksummed, and a FAILED mixed-case checksum is refused rather
    than normalized. SVM: returned byte-for-byte, because there is nothing to
    normalize and any change would be a different address.
    """
    family = family_of(chain)
    if family is None:
        from core.wallet import chains
        raise ValueError(
            f"unknown chain {chain!r} — known chains: {', '.join(chains.names())}. "
            f"Refusing rather than assuming a family: validating a Solana mint "
            f"as an Ethereum address (or the reverse) is how an address ends up "
            f"checked against the wrong chain entirely.")
    if family == "evm":
        from core.wallet.tokens import normalize_address
        return normalize_address(addr)
    if family == "svm":
        if not isinstance(addr, str):
            raise ValueError(f"not a Solana address: {addr!r}")
        if addr.startswith("0x"):
            raise ValueError(
                f"{addr!r} is a hex address, but {chain} uses base58 account "
                f"keys — this address belongs to a different chain")
        raw = b58decode(addr)          # raises on an illegal character
        if len(raw) != _SOLANA_KEY_BYTES:
            raise ValueError(
                f"a Solana account key is {_SOLANA_KEY_BYTES} bytes; {addr!r} "
                f"decodes to {len(raw)}")
        return addr
    raise ValueError(f"no address rules for chain family {family!r}")


def typo_protection_note(chain: str) -> str:
    """What a caller can and cannot rely on when it echoes an address back.

    Deliberately a sentence rather than a boolean: the point is for the text an
    agent or an owner reads to say which world it is in, because the EVM habit
    ("a wrong address would have failed the checksum") is simply false on
    Solana and carrying it across is how funds go somewhere real by accident.
    """
    family = family_of(chain)
    if family == "evm":
        return ("EIP-55 checksum verified — a mistyped address of this shape "
                "would almost certainly have been refused.")
    if family == "svm":
        return ("base58 has NO checksum, so a mistyped address that still "
                "decodes to 32 bytes is a valid, DIFFERENT account and nothing "
                "here can flag it. Verify it against an independent source "
                "before anything is sent to it.")
    return "unknown chain — no address rules apply."
