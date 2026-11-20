"""A record the owner seat writes and the wallet key signs (the mandate, the income rule).

ONE home for the pattern both stores use (``core/wallet/standing_grant.py``,
``core/wallet/income_rule.py``): the record is a dataclass with a ``sig`` field; the
wallet's operational key signs ``prefix + sha256(every other field)``; a record a shell
wrote, or one whose fields changed, does not verify and grants nothing. Only the owner
seat writes one (never an agent turn, never an autonomous owner-tenant run).

Each store keeps its own prefix, error class and words, and its own ``_signer`` name
(``= wallet_signer``) so a test can replace the key.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Optional


def wallet_signer():
    """The wallet's operational signer, or None (no wallet = nothing is signed)."""
    try:
        from core.wallet.factory import get_agent_wallet
        w = get_agent_wallet()
        return w.operational_signer() if w is not None else None
    except Exception:  # noqa: BLE001
        return None


def digest(record: Any) -> bytes:
    """The hex sha256 of every field but ``sig``."""
    body = {k: v for k, v in asdict(record).items() if k != "sig"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest().encode()


def sign(record: Any, prefix: bytes, signer: Any, error: type, *, noun: str,
         remote: str) -> None:
    """Set ``record.sig``, or raise *error*: no wallet, or a key that cannot sign. *noun*
    names the record (``the mandate``); *remote* says what that means under
    ``WALLET_SIGNER=remote`` (audit 2026-11-21 L-5: that signer signs no record yet)."""
    if signer is None:
        raise error(f"the agent wallet is off — {noun} is signed by its key")
    try:
        record.sig = signer.sign_message(prefix + digest(record))
    except Exception as exc:  # noqa: BLE001 — the signer names its own refusal
        raise error(f"the wallet key could not sign {noun} ({exc}). Under "
                    f"WALLET_SIGNER=remote {remote}") from exc


def verified(record: Any, prefix: bytes, signer: Any) -> bool:
    """True when ``record.sig`` is the wallet key's signature of the record as it is."""
    if signer is None or not getattr(record, "sig", ""):
        return False
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        got = Account.recover_message(encode_defunct(prefix + digest(record)),
                                      signature=record.sig)
        return str(got).lower() == str(signer.address).lower()
    except Exception:  # noqa: BLE001
        return False


def seat_refusal(ctx: Any, verb: str, *, does: str, public: str, denied: str) -> Optional[str]:
    """Why *ctx* may not write the record, or None: the owner's turn rule, then the owner
    SEAT only — an autonomous owner-tenant run passes the first and fails here (audit
    2026-11-21 L-4). *denied* ends the seat sentence."""
    from core.security.owner_turn import owner_turn_refusal
    from core.security.turn_facts import is_owner_seat
    refusal = owner_turn_refusal(ctx, verb=verb, does=does, public=public)
    if refusal:
        return refusal
    if not is_owner_seat(ctx):
        return f"{verb} denied: {denied}"
    return None


__all__ = ["digest", "seat_refusal", "sign", "verified", "wallet_signer"]
