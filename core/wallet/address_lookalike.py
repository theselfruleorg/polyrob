"""Address poisoning: a payee that only LOOKS like one we already paid.

The attack: right after a payment, the attacker sends dust from a vanity address
whose first and last hex characters match the real payee, so it sits in the
wallet history next to the real one. The next payment copied from history goes
to the attacker. Prod 2026-10-04 17:46 saw two of these within minutes of the
owner's payments.

The heuristic checks matching ends, a long matching prefix/suffix and a few
changed characters. Missing history is empty; unreadable history refuses the
send. This assists full-address verification and cannot prove a payee's identity.
"""
from __future__ import annotations

import json
import logging
import re
import time
import math
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

#: How far back a payee counts as "recently paid".
WINDOW_SEC = 30 * 24 * 3600
_HEAD = 3  # prod 2026-10-04: the 0x45d4… fake matched only 3 head chars of 0x45dd…
_TAIL = 4
_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
#: The audit-ledger ``action`` of every EVM send to a payee: a token/native
#: transfer, ``nft_transfer`` (trade_tool) and the agent-NFT withdrawal, which
#: ``tools/agent_nft/guarded.py`` records as ``agent_nft_<verb>``.
_EVM_SEND_ACTIONS = frozenset({"transfer", "nft_transfer", "agent_nft_withdraw_token"})


def _norm(addr, chain: str = "evm") -> Optional[str]:
    s = str(addr or "").strip()
    if chain == "solana":
        return s if _SOLANA.fullmatch(s) else None
    return s.lower() if _EVM.match(s) else None


def lookalike_of(to: str, known: Iterable, *, chain: str = "evm") -> Optional[str]:
    """The known address `to` imitates (same head and tail, different body), or None."""
    t = _norm(to, chain)
    if t is None:
        return None
    body = t if chain == "solana" else t[2:]
    for k in known:
        n = _norm(k, chain)
        if n is None or n == t:
            continue
        kb = n if chain == "solana" else n[2:]
        matching_ends = kb[:_HEAD] == body[:_HEAD] and kb[-_TAIL:] == body[-_TAIL:]
        similar = len(kb) == len(body) and sum(a != b for a, b in zip(kb, body)) <= 3
        long_end = kb[:8] == body[:8] or kb[-8:] == body[-8:]
        if matching_ends or similar or long_end:
            return str(k)
    return None


def paid_counterparties(*, path: Optional[str] = None,
                        now: Optional[float] = None, chain: str = "evm") -> List[str]:
    """Recipients of our recorded transfers within the window (newest last)."""
    if path is None:
        from core.wallet.trade_index import audit_path
        path = audit_path()
    cutoff = (now if now is not None else time.time()) - WINDOW_SEC
    out: List[str] = []
    try:
        from pathlib import Path
        from core.security.workspace_io import read_bytes
        limit = 64 * 1024 * 1024
        raw = read_bytes(path, Path(path).parent, max_bytes=limit + 1)
        if len(raw) > limit:
            raise ValueError("wallet audit exceeds the lookalike scan limit")
    except FileNotFoundError:
        return []
    seen = set()
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("wallet audit row is not an object")
        actions = {"transfer", "solana_transfer"} if chain == "solana" else _EVM_SEND_ACTIONS
        if row.get("action") not in actions:
            continue
        ts = float(row["ts"])
        if not math.isfinite(ts):
            raise ValueError("wallet audit timestamp is invalid")
        if ts < cutoff:
            continue
        cp = row.get("counterparty")
        if _norm(cp, chain) and cp not in seen:
            out.append(cp)
            seen.add(cp)
    return out


def poisoning_refusal(to: str, *, path: Optional[str] = None,
                      now: Optional[float] = None, chain: str = "evm") -> Optional[str]:
    """The refusal text when `to` imitates a recent payee, else None."""
    try:
        real = lookalike_of(to, paid_counterparties(path=path, now=now, chain=chain), chain=chain)
    except Exception:
        logger.warning("wallet history is unreadable for the lookalike check")
        return "refused: wallet history is unreadable; repair the audit ledger before sending. Nothing was sent."
    if real is None:
        return None
    return (f"refused: {to} looks like {real}, which we recently paid. Their full "
            f"addresses differ despite similar characters: a likely ADDRESS-"
            f"POISONING lookalike. Nothing was sent. Re-check the full address "
            f"against the owner's CURRENT message; never take a payee from wallet history.")
