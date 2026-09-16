"""Signer seam: produces ECDSA / EIP-712 signatures for ONE agent address.

LocalEoaSigner and its SDK account adapter hold keys in the agent interpreter.
This is an integration seam, not a security boundary: trusted in-process code
can read the key and bypass callers' policy. A separate signer must independently
authorize transactions and non-transaction signatures; a matching Python
Protocol alone does not establish that isolation.
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
    def sign_transaction(self, tx: dict) -> bytes: ...


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

    def sign_transaction(self, tx: dict) -> bytes:
        """Sign a transaction dict, returning the raw signed bytes.

        These checks constrain this method only; the SDK account adapter and
        other code in this interpreter retain direct signing authority.
        Non-transaction signatures can also transfer spending authority.

        * ``chainId`` is MANDATORY. An unpinned chainId is EIP-155 replay
          exposure — the same signed payload would be valid on every EVM chain
          the address exists on (i.e. all of them).
        * ``sign_typed_data`` must stay OFF the money path. A signed EIP-2612 /
          Permit2 payload is not a transaction, so it never reaches tx_guard: no
          simulation, no delta assertion, no cap, no audit row, and the drain
          happens later in a transaction the agent never sees. Permit support is
          deliberately out of scope; if it is ever added it must be priced as an
          allowance grant and routed through the guard.
        """
        if not tx.get("chainId"):
            raise ValueError(
                "refusing to sign a transaction with no chainId — an unpinned "
                "chain is EIP-155 replay exposure")
        signed = self._account.sign_transaction(dict(tx))
        return bytes(signed.raw_transaction)

    def __repr__(self) -> str:  # never leak the key
        return f"<LocalEoaSigner address={self._account.address}>"
