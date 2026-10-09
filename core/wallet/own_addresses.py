"""Addresses that are OURS: the owner's configured receive addresses and the
agent's own wallets.

A transfer to one of these cannot move value to a third party, so a genuine
owner turn that sends there needs no second owner tap (the caps and the guard
still apply). The owner's list is ``OWNER_WALLET_ADDRESSES`` — an env flag the
console cannot write (OWNER segment), so the agent can never add to it.
Equality is :func:`core.wallet.addresses.same_address` (case-folded for 0x-hex
only; base58 is case-sensitive).
"""
from __future__ import annotations

import os
from typing import List, Optional

OWNER_LABEL = "the owner's configured address (OWNER_WALLET_ADDRESSES)"
AGENT_LABEL = "the agent's own wallet"


def owner_wallet_addresses() -> List[str]:
    """``OWNER_WALLET_ADDRESSES``, a comma list; empty when unset."""
    raw = os.environ.get("OWNER_WALLET_ADDRESSES", "")
    return [a.strip() for a in raw.split(",") if a.strip()]


def agent_wallet_addresses() -> List[str]:
    """The agent wallet's EVM and Solana addresses that resolve (fail-soft)."""
    out: List[str] = []
    try:
        from core.wallet.factory import get_agent_wallet
        wallet = get_agent_wallet()
    except Exception:
        return out
    if wallet is None:
        return out
    for attr in ("address", "solana_address"):
        try:
            value = getattr(wallet, attr, None)
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return out


def own_destination(to) -> Optional[str]:
    """Which of OUR addresses *to* is, or None for anyone else's."""
    if not isinstance(to, str) or not to.strip():
        return None
    from core.wallet.addresses import same_address
    if any(same_address(to, a) for a in owner_wallet_addresses()):
        return OWNER_LABEL
    if any(same_address(to, a) for a in agent_wallet_addresses()):
        return AGENT_LABEL
    return None
