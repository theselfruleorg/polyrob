"""Detect funds sitting on a Solana account the agent does NOT use.

The agent has exactly ONE Solana identity: ``wallet.solana_signer()``, account
index 0. Every consumer goes through it — `portfolio`, `x402_wallet_status`,
`reconcile`, `treasury_balance_usd`, `defi_trade.solana_swap`.

But the SEED derives an unbounded family of accounts, and a script that wanted a
counterparty helped itself to index 1 (`scripts/x402_solana_roundtrip.py`, which
pays the agent's treasury from its own "payer" account to exercise the invoice
loop). On 2026-08-25 that script's funding instruction was read as a request to
fund the agent, and it received 1 SOL. The money was never lost — the key
derives from the same seed — but nothing in the system reads index 1, so for
three days the agent reported "gas (SOL): 0 — nothing can be sent from here"
while holding ~$200 of SOL one derivation index away.

The lesson is not "index 1 is bad". It is that a wallet which DERIVES many
accounts and READS one will eventually be blind to its own money, and the blind
spot is silent — an empty canonical account looks exactly like a broke agent.
This module is the check that makes it loud.

Deliberately a scan of a small fixed window rather than an exhaustive search:
the point is to catch a stray deposit near the canonical account, which is where
a human or a script realistically puts one. It costs one RPC round trip per
index and is fail-open — a scan that cannot run reports nothing found rather
than blocking the caller.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: How many NON-canonical derivation indices to check (1..N). Small on purpose:
#: this is a stray-deposit check, not a wallet recovery sweep.
DEFAULT_SCAN_DEPTH = 3

#: Below this, a balance is rounding dust from a closed token account and not
#: worth alarming the owner about. Well under a cent of SOL.
DUST_LAMPORTS = 100_000  # 0.0001 SOL


@dataclass(frozen=True)
class StrayAccount:
    """A derived Solana account holding value that the agent does not read."""
    index: int
    address: str
    sol: Optional[float]
    tokens: Dict[str, int]

    @property
    def has_value(self) -> bool:
        if self.sol is not None and self.sol * 1e9 > DUST_LAMPORTS:
            return True
        return any(amount > 0 for amount in (self.tokens or {}).values())


def find_stray_accounts(wallet, *, depth: int = DEFAULT_SCAN_DEPTH,
                        native_fn=None, tokens_fn=None) -> List[StrayAccount]:
    """Derived accounts 1..*depth* that hold value. Index 0 is never included —
    that one IS the agent, and value there is not stray.

    Fail-open: any derivation or RPC failure yields no finding for that index
    rather than raising. A missed warning is bad; breaking `wallet_status` for
    every caller because one RPC hiccuped is worse.
    """
    from core.wallet import solana_onchain
    native = native_fn or solana_onchain.native_balance
    tokens = tokens_fn or solana_onchain.token_balances

    found: List[StrayAccount] = []
    for index in range(1, max(1, int(depth)) + 1):
        try:
            address = wallet.solana_signer(index).address
        except Exception:
            logger.debug("stray scan: cannot derive solana account %s", index,
                         exc_info=True)
            continue
        try:
            sol = native(address)
        except Exception:
            sol = None
        try:
            raw = tokens(address) or {}
        except Exception:
            raw = {}
        candidate = StrayAccount(index=index, address=address, sol=sol,
                                 tokens={m: a for m, a in raw.items() if a > 0})
        if candidate.has_value:
            found.append(candidate)
    return found


def format_stray_warning(strays: List[StrayAccount],
                         canonical_address: str) -> Optional[str]:
    """One owner-readable warning block, or None when there is nothing to say."""
    if not strays:
        return None
    lines = ["⚠️  FUNDS ON A NON-AGENT SOLANA ACCOUNT — the agent cannot see or "
             "spend these. Same seed, different derivation index, so nothing is "
             "lost; it is in the wrong place."]
    for s in strays:
        bits = []
        if s.sol:
            bits.append(f"SOL {s.sol:.9f}")
        for mint, amount in sorted(s.tokens.items()):
            bits.append(f"{mint} {amount} raw units")
        lines.append(f"    index {s.index}: {s.address}")
        lines.append(f"      {'  '.join(bits) or 'value present'}")
    lines.append(f"    The agent's ONE address is {canonical_address} (index 0).")
    lines.append("    Move the balance there; nothing in the system reads any "
                 "other index.")
    return "\n".join(lines)
