"""086 W0 §3.10: retire a book row the chain no longer backs — with proof.

``/writeoff`` is the owner's LOSS verdict. A row whose tokens are simply gone
(sold outside the rail, a 0.16-token remainder, a rounding tail) had no honest
way to close: the ROBIN row blocked a goal for 10 days. ``retire_row`` closes
one ONLY when a fresh chain measurement shows a zero balance, or a balance worth
under :data:`core.open_positions.DUST_USD`, and stores that measurement on the
row (``status_proof``, JSON) so the close is evidence, never a model's say-so.

The caller MEASURES; this module never trusts a number it was not handed by a
measurement, and it never reads the chain (core tier). The owner verb
(``/book retire``) and the agent verb (``defi_data.retire_row``) both measure
through ``tools/defi/retire.py`` and land here.

The row is kept (status ``retired``, cost basis as history): it is excluded
from :func:`core.open_positions.positions_for` by default, a later sell of it is
a no-op, and a later buy starts a fresh row.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from typing import Optional

from core import open_positions as _op


@dataclass(frozen=True)
class RetireOutcome:
    """``retired`` = the row changed. ``reason`` says why not, or what was done."""
    retired: bool
    reason: str


def _finite(x) -> Optional[float]:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def retire_refusal(measured_qty, measured_usd) -> Optional[str]:
    """Why a measurement does not permit a retire, or None. Pure."""
    qty = _finite(measured_qty)
    if qty is None or qty < 0:
        return ("no usable balance measurement — a retire needs the chain's "
                "balance, read now; unknown is not zero")
    if qty == 0:
        return None
    usd = _finite(measured_usd)
    if usd is None or usd < 0:
        return (f"the chain still holds {qty:,.6g} and its value is UNKNOWN, so it "
                f"cannot be called dust; sell it or write it off instead")
    if usd >= _op.DUST_USD:
        return (f"the chain still holds {qty:,.6g} worth ${usd:,.2f} — not dust "
                f"(under ${_op.DUST_USD:,.2f}); sell it or write it off instead")
    return None


def retire_row(user_id: str, chain: str, address: str, *, account: str,
               measured_qty, measured_usd, proof: str,
               data_dir: Optional[str] = None, db_path: Optional[str] = None,
               now: Optional[float] = None) -> RetireOutcome:
    """Close one tenant's row as ``retired`` on a measured zero/dust balance.

    ``measured_qty`` (human units) and ``measured_usd`` (``None`` = unpriced)
    come from a chain read the CALLER just made; ``proof`` names that read
    (source, raw units, block/slot or time). Never creates the store."""
    uid = str(user_id or "").strip()
    chain_key = str(chain or "").strip().lower()
    if not uid or not chain_key or not address:
        return RetireOutcome(False, "no tenant, chain or address")
    proof_text = str(proof or "").strip()
    if not proof_text:
        return RetireOutcome(False, "no proof — a retire records the measurement it rests on")
    why = retire_refusal(measured_qty, measured_usd)
    if why:
        return RetireOutcome(False, why)
    path = db_path or _op.open_positions_db_path(data_dir)
    if not os.path.isfile(path):
        return RetireOutcome(False, f"I track no position at {address} on {chain_key}")
    pos = _op.get_position(uid, chain_key, address, db_path=path, account=account)
    if pos is None:
        # A chain name in another case: the store keys the chain as written.
        pos = next((p for p in _op.positions_for(uid, account=account, db_path=path,
                                                 statuses=_op.STATUSES)
                    if p.chain.lower() == chain_key
                    and _op._norm(p.address) == _op._norm(address)), None)
    if pos is None:
        return RetireOutcome(False, f"I track no position at {address} on {chain_key}")
    if pos.status == _op.STATUS_RETIRED:
        return RetireOutcome(False, f"{pos.symbol or address} on {chain_key} is already retired")
    ts = time.time() if now is None else now
    qty = _finite(measured_qty)
    usd = _finite(measured_usd)
    record = {"measured_qty": qty, "measured_usd": usd, "proof": proof_text[:600],
              "ts": ts, "dust_usd": _op.DUST_USD, "book_qty": pos.qty,
              "prior_status": pos.status}
    reason = (f"retired: chain holds {qty:,.6g}"
              + (f" (${usd:,.2f})" if usd is not None else "") + f" — {proof_text}")
    from core.sqlite_util import execute_retry
    _op._init(path)
    changed = execute_retry(
        path,
        "UPDATE open_positions SET status=?, status_reason=?, status_ts=?, status_proof=? "
        "WHERE user_id=? AND chain=? AND account=? AND address=? AND status<>?",
        (_op.STATUS_RETIRED, reason[:300], ts, json.dumps(record), uid, pos.chain,
         _op._account_key(account), pos.address, _op.STATUS_RETIRED))
    if not changed:
        return RetireOutcome(False, "the store did not record the retire")
    # 086 W1: after the commit — the lifecycle disarms the row's exit job.
    from core import position_events
    position_events.emit(position_events.RETIRED, user_id=uid, chain=pos.chain,
                         account=_op._account_key(account), address=pos.address,
                         symbol=pos.symbol, qty=pos.qty, usd=usd, reason="retired", ts=ts)
    return RetireOutcome(True, reason)


def retire_proof(user_id: str, chain: str, address: str, *, account: str = "",
                 db_path: Optional[str] = None,
                 data_dir: Optional[str] = None) -> Optional[dict]:
    """The stored measurement of a retired row, or None. Read-only."""
    path = db_path or _op.open_positions_db_path(data_dir)
    if not os.path.isfile(path):
        return None
    from core.sqlite_util import execute_retry
    try:
        _op._ensure_readable(path)
        row = execute_retry(
            path,
            "SELECT status_proof FROM open_positions WHERE user_id=? AND lower(chain)=? "
            "AND account=? AND address=?",
            (str(user_id or "").strip(), str(chain or "").strip().lower(),
             _op._account_key(account), _op._norm(address)), fetch="one")
    except Exception:
        return None  # a store older than the column (read-only unit) holds no proof
    if not row or not row["status_proof"]:
        return None
    try:
        return json.loads(row["status_proof"])
    except (TypeError, ValueError):
        return None


__all__ = ["RetireOutcome", "retire_row", "retire_proof", "retire_refusal"]
