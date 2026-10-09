"""Read-only lookup over the wallet's own recorded money movements.

The settlement watcher has to answer one question about an inbound USDC
transfer that matches no pending invoice: *did WE cause this?* A token sell's
proceeds land in the treasury as a plain inbound `Transfer`, on-chain
indistinguishable from a stray payment, so without an answer every sell fired a
`payment_unmatched` owner notice.

The first answer was address-only: skip anything whose `from` is the chain's
Uniswap router or LI.FI aggregator. But those are SHARED PUBLIC infrastructure
— every wallet on the chain uses them — so a genuine payer who routed through
the same router, or who overpaid and missed the exact-amount match, was
silently dropped: no settlement, no unmatched notice, no telemetry. A heuristic
that swallows a real payment is worse than the noise it removes.

This module gives the honest answer instead. `PolicyGate.record` writes the
broadcast reference of every value-moving action to the append-only wallet audit
(`result_ref`), and for a swap that reference IS the transaction that carries
the proceeds home. So the correlation is an exact hash match against our own
ledger, and "not in the ledger" means "not confirmed as ours" — which sends the
transfer back to the unmatched-payment path rather than into a silent skip.

Read-only and side-effect free by construction: the file is opened for reading,
never created, and a missing or unreadable ledger yields an EMPTY set, so the
caller falls back to notifying the owner.
"""
from __future__ import annotations

import json
import logging
import os
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

#: The audit venue money-moving DeFi actions record under (`defi_trade`'s
#: swap/transfer/approve verbs and `solana_swap`). Scoping to it keeps the
#: correlation to the actions that can actually produce router-sourced
#: proceeds, rather than every entry the ledger has ever held.
TRADE_VENUE = "defi"


def audit_path(data_dir: Optional[str] = None) -> str:
    from core.wallet.audit_sink import _wallet_data_dir
    return os.path.join(_wallet_data_dir(data_dir), "audit.jsonl")


def own_trade_tx_refs(data_dir: Optional[str] = None,
                      path: Optional[str] = None,
                      account: Optional[str] = None) -> Set[str]:
    """Lower-cased broadcast references of our own recorded trades.

    An empty set is the honest answer for a missing/unreadable ledger: it means
    "nothing is confirmed as ours", never "nothing is ours".

    ``account`` (050 §7.4): ``None`` = every holder (the pre-050 answer);
    ``""`` = the treasury only (entries with no ``account``); an address = that
    token-bound account only. An account's sell lands its proceeds in the ACCOUNT, so the
    treasury's settlement watcher must not count it as the treasury's own.
    """
    ledger = path or audit_path(data_dir)
    refs: Set[str] = set()
    if not os.path.exists(ledger):
        return refs
    try:
        with open(ledger, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue            # one corrupt line never hides the rest
                if not isinstance(entry, dict):
                    continue
                if entry.get("venue") != TRADE_VENUE:
                    continue
                if account is not None and \
                        str(entry.get("account") or "").lower() != str(account).lower():
                    continue
                ref = entry.get("result_ref")
                if ref:
                    refs.add(str(ref).strip().lower())
    except OSError as exc:
        logger.warning(
            "wallet trade index: could not read %s (%s) — an inbound transfer "
            "cannot be confirmed as our own trade proceeds and will be treated "
            "as an unmatched payment", ledger, exc)
        return set()
    return refs


def is_own_trade_tx(tx_hash: Optional[str], data_dir: Optional[str] = None,
                    path: Optional[str] = None, account: Optional[str] = None) -> bool:
    """Did WE broadcast *tx_hash*? Case-insensitive (an EVM hash reaches the
    ledger from web3 and the scanner from `eth_getLogs`, in different cases)."""
    if not tx_hash:
        return False
    return str(tx_hash).strip().lower() in own_trade_tx_refs(data_dir, path, account)


#: Audit-ledger actions that pay money OUT to a counterparty.
PAYMENT_ACTIONS = frozenset({"transfer", "solana_transfer"})


def _payee_key(addr) -> str:
    s = str(addr or "").strip()
    return s.lower() if s[:2].lower() == "0x" else s


def own_transfers_to(counterparty: Optional[str], data_dir: Optional[str] = None,
                     path: Optional[str] = None) -> Optional[List[dict]]:
    """Our own recorded outbound transfers to *counterparty*, oldest first.

    Answers "did we pay X?" from the ledger (prod 2026-10-05: Rob found "no
    record" of an 80 USDC payment it had made itself, then invented a cause).
    Every outbound payment action counts (:data:`PAYMENT_ACTIONS`): a Solana
    send is recorded as ``solana_transfer``, and reading ``transfer`` alone
    answered "no record" for every Solana payee. An EVM (``0x``) address
    matches case-insensitively; a base58 address is case-sensitive and matches
    exactly. ``None`` means the ledger could not be read — UNKNOWN, never "we
    paid nothing".
    """
    want = _payee_key(counterparty)
    ledger = path or audit_path(data_dir)
    if not want or not os.path.exists(ledger):
        return None
    out: List[dict] = []
    try:
        with open(ledger, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict) or entry.get("action") not in PAYMENT_ACTIONS:
                    continue
                if _payee_key(entry.get("counterparty")) != want:
                    continue
                out.append({k: entry.get(k) for k in
                            ("ts", "amount_usd", "asset", "chain", "result_ref", "lane")})
    except OSError as exc:
        logger.warning("wallet trade index: could not read %s (%s)", ledger, exc)
        return None
    return out
