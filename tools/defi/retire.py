"""086 W0 §3.10: retire a book row on a FRESH chain measurement.

The one measuring path behind both retire entries:
- the owner verb ``/book retire <chain> <address> [go]`` (Telegram + REPL), and
- the agent verb ``defi_data.retire_row(chain, address)`` (``data_retire.py``).

Neither takes a balance from its caller. This module reads the operator wallet's
balance of the token itself — through the SAME seams ``reconcile`` reads
(``DefiDataTool._balances`` on EVM, ``_solana_tokens`` on Solana) — values it
only at a HIGH-confidence price, and hands the measurement to
``core.position_retire.retire_row``, which refuses anything but a zero or
dust balance and stores the measurement as proof.

UNKNOWN is never zero: a failed balance read, unknown decimals, or a non-zero
balance with no high-confidence price all refuse.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class Measurement:
    raw: Optional[int] = None
    qty: Optional[float] = None
    usd: Optional[float] = None
    proof: str = ""
    error: Optional[str] = None


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def measure(tool, chain: str, address: str, *, now: Optional[float] = None) -> Measurement:
    """The operator wallet's balance of ``address`` on ``chain``, read now."""
    from core.wallet import chains
    from core.wallet.addresses import same_address
    from core.wallet.tokens import bounded_decimals
    row = chains.get(chain)
    if row is None:
        return Measurement(error=f"chain {chain!r} is not supported")
    if row.wrapped_native and same_address(row.wrapped_native, address):
        return Measurement(error=(
            f"{address} is {chain}'s wrapped native — working capital whose balance "
            f"gas and wraps move; a major row is closed by selling it, not retired"))
    svm = row.family == "svm"
    holder = tool._solana_holder() if svm else tool._resolve_holder()
    if not holder:
        from core.wallet.config import WALLET_OFF_TEXT
        return Measurement(error=WALLET_OFF_TEXT + " — no wallet to measure")
    ts = time.time() if now is None else now
    if svm:
        held = tool._solana_tokens(holder)
        if held is None:
            return Measurement(error=("the Solana token-account read FAILED — the balance "
                                      "is UNKNOWN, not zero; retry"))
        # getTokenAccountsByOwner enumerates every SPL balance: absent = zero.
        raw = next((int(v) for k, v in held.items() if k == address and v is not None), 0)
        source = "getTokenAccountsByOwner (complete enumeration)"
    else:
        got = tool._balances(holder, chain, [address]) or {}
        vals = [v for k, v in got.items() if same_address(k, address)]
        if not vals or vals[0] is None:
            return Measurement(error=(f"the balance read of {address} on {chain} FAILED — "
                                      f"UNKNOWN, not zero; retry"))
        raw = int(vals[0])
        source = "balanceOf"
    if raw < 0:
        return Measurement(error="the chain returned a negative balance — not a measurement")
    proof = f"{source} on {chain} for {holder} at {_iso(ts)}: {raw} raw"
    if raw == 0:
        return Measurement(raw=0, qty=0.0, usd=0.0, proof=proof)
    ident = tool._identity(chain, address)
    dec = bounded_decimals(getattr(ident, "decimals", None))
    if dec is None:
        return Measurement(raw=raw, proof=proof, error=(
            f"the chain holds {raw} raw units and the token's decimals are unknown, "
            f"so the size and value are UNKNOWN — not dust"))
    qty = raw / (10 ** dec)
    usd = None
    try:
        info = tool._price_for(chain, address)
    except Exception:
        info = None
    price = getattr(info, "price_usd", None) if info is not None else None
    if (price is not None and getattr(info, "confidence", None) == "high"
            and float(price) > 0):
        usd = qty * float(price)
        proof += f" = {qty:,.6g} at ${float(price):.6g} (high confidence) = ${usd:,.4f}"
    else:
        proof += f" = {qty:,.6g}; no high-confidence price"
    return Measurement(raw=raw, qty=qty, usd=usd, proof=proof)


def retire_with_proof(user_id: str, chain: str, address: str, *, execute: bool,
                      tool=None, db_path: Optional[str] = None,
                      data_dir: Optional[str] = None,
                      account: str = "") -> Tuple[bool, str]:
    """Measure, then retire (``execute``) or say what a retire would do.

    Returns ``(ok, text)``; ``ok`` is False on any refusal."""
    from core import open_positions as op
    from core.position_retire import retire_refusal, retire_row
    chain = str(chain or "").strip().lower()
    uid = str(user_id or "").strip()
    if not uid:
        return False, "no tenant — I cannot tell whose book to change"
    if tool is None:
        from tools.defi.data_tool import DefiDataTool
        tool = DefiDataTool()
    canonical, err = tool._validate(chain, address)
    if err:
        return False, err
    path = db_path or op.open_positions_db_path(data_dir)
    pos = next((p for p in op.positions_for(uid, account=account, db_path=path,
                                            statuses=op.STATUSES)
                if p.chain.lower() == chain and op._norm(p.address) == op._norm(canonical)),
               None)
    if pos is None:
        return False, (f"I track no position at {canonical} on {chain}. /book lists "
                       f"what the rail tracks.")
    label = f"{pos.symbol or '?'} {pos.address} on {chain}"
    if pos.status == op.STATUS_RETIRED:
        return False, f"{label} is already retired."
    m = measure(tool, chain, canonical)
    if m.error:
        return False, f"Not retired: {m.error}. The row stays {pos.status}."
    why = retire_refusal(m.qty, m.usd)
    if why:
        return False, (f"Not retired: {why}. Measured: {m.proof}. The row stays "
                       f"{pos.status}.")
    basis = ("basis unknown" if pos.entry_usd is None
             else f"recorded cost ${pos.entry_usd:,.2f}")
    if not execute:
        return True, (f"Retire {label} (book {pos.qty:,.6g}, {basis})?\n"
                      f"The chain shows it is gone or dust — {m.proof}.\n"
                      f"The row closes as retired with that proof; its cost stays as "
                      f"history; nothing is sold or sent. Confirm:\n"
                      f"/book retire {chain} {pos.address} go")
    out = retire_row(uid, pos.chain, pos.address, account=account, measured_qty=m.qty,
                     measured_usd=m.usd, proof=m.proof, db_path=path)
    if not out.retired:
        return False, f"Not retired: {out.reason}."
    return True, (f"✅ Retired {label}. Proof: {m.proof}. The book no longer lists it; "
                  f"a later buy starts a fresh row.")


def _split(args: List[str]) -> Tuple[List[str], bool]:
    words = [str(a) for a in (args or []) if str(a).strip()]
    go = len(words) > 2 and words[2].lower() in ("go", "confirm")
    return words[:2], go


async def book_retire_reply(user_id: Optional[str], data_dir: Optional[str],
                            args: List[str], *, tool=None) -> str:
    """``/book retire <chain> <address> [go]`` — the owner seats' one handler.
    ``args`` are the words AFTER ``retire``."""
    if not user_id:
        return "Only the owner retires a book row."
    head, go = _split(args)
    if len(head) < 2:
        return ("Usage: /book retire <chain> <address> [go]\n"
                "Closes a row the chain shows is gone or dust (under $1), with the "
                "balance read as proof. Bare = what it would do; `go` records it. "
                "A holding worth more is sold or written off (/writeoff), never retired.")
    from core import open_positions as op
    db_path = op.open_positions_db_path(data_dir) if data_dir else None
    _ok, text = await asyncio.to_thread(
        retire_with_proof, user_id, head[0], head[1], execute=go, tool=tool,
        db_path=db_path)
    return text


__all__ = ["Measurement", "measure", "retire_with_proof", "book_retire_reply"]
