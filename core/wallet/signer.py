"""Signer seam: produces ECDSA / EIP-712 signatures for ONE agent address.

The raw private key NEVER crosses this boundary outward. LocalEoaSigner is the
self-custody backend; CdpSigner/TurnkeySigner (deferred) implement the same
Protocol so the custody backend is swappable without touching venue tools.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data


@runtime_checkable
class Signer(Protocol):
    @property
    def address(self) -> str: ...
    def sign_message(self, data: bytes) -> str: ...
    def sign_typed_data(self, domain: dict, types: dict, message: dict) -> str: ...


class LocalEoaSigner:
    """Self-custody EOA signer wrapping an eth_account LocalAccount."""

    def __init__(self, private_key: bytes):
        self._account = Account.from_key(private_key)

    @property
    def address(self) -> str:
        return self._account.address

    @property
    def account(self):
        """The native LocalAccount, for SDKs that require it (e.g. Hyperliquid).

        In-process use only — never returned from a tool action / logged."""
        return self._account

    def sign_message(self, data: bytes) -> str:
        signed = self._account.sign_message(encode_defunct(data))
        return signed.signature.hex() if signed.signature.hex().startswith("0x") else "0x" + signed.signature.hex()

    def sign_typed_data(self, domain: dict, types: dict, message: dict) -> str:
        signable = encode_typed_data(domain_data=domain, message_types=types, message_data=message)
        signed = self._account.sign_message(signable)
        sig = signed.signature.hex()
        return sig if sig.startswith("0x") else "0x" + sig

    def __repr__(self) -> str:  # never leak the key
        return f"<LocalEoaSigner address={self._account.address}>"
