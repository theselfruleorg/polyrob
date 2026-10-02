"""Rail preflight — a $0 skip when a DETERMINISTIC precondition is already
false before any model call (2026-09-21, intel deep review).

The SCOUT rail ran 24×/24 h and could not enter ONCE by its own rule — every
run's closing memory read "NO ENTRY — rule 10 slot cap (7 rows vs 6)". That was
$1.07/day (27 % of the day's spend) re-deriving a fact the ledger already
states. The precondition is readable in-process before the run:

    payload.preflight = {"kind": "slot_cap", "max_open_rows": 6}

reads the `## Open positions` row count through the ONE ledger reader
(`core.position_ledger.read_open_positions`) and, when the book is at or
over the cap, the tick is emitted as `cron_run skipped/no_slot` with the
counts, exactly the shape of `wake_agent=false` / `no_change`. The EXIT rail's
row write IS the release: a freed slot changes the count, so the very next
tick runs.

Unlike the wake change-gate this is MEANT for money rails — it reads the
rail's own precondition, not a fingerprint that never moves with a price.

Fail-OPEN by construction: an unreadable ledger, an unknown kind, a bad cap or
a malformed payload runs the tick. A gate that silently skipped a money rail
on a read error would be the 09-21 reconcile outage in another shape.
Delivery jobs are never preflighted (their run IS the delivery).

A second, UNDECLARED check runs first for every job: a rail whose credential
has an open verdict (``cron/credential_preflight.py``) — the X DM crons kept
paying for turns for days after the X login died (2026-09-26 evaluation).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

KIND_SLOT_CAP = "slot_cap"


@dataclass(frozen=True)
class PreflightSkip:
    reason: str
    attrs: Dict[str, Any] = field(default_factory=dict)
    #: an owner-facing notice the runner delivers with this skip (the credential
    #: preflight sets it on the FIRST skip of a verdict episode only)
    notice: Optional[str] = None


def _slot_cap(spec: Dict[str, Any], *, data_dir: str) -> Optional[PreflightSkip]:
    try:
        cap = int(spec.get("max_open_rows"))
    except (TypeError, ValueError):
        return None
    if cap <= 0:
        return None
    from core.position_ledger import open_positions, read_open_positions
    parsed, err = read_open_positions(data_dir)
    if err:
        logger.info("preflight slot_cap: ledger unreadable (%s) — running the tick", err)
        return None
    # Rows that still occupy a slot, not rows in the table: a full exit recorded
    # in place (`Size` edited to `0 — FULL EXIT …`) is bookkeeping, not a
    # position, and counting it cost SCOUT a genuinely free slot on 2026-09-23.
    rows = open_positions(parsed)
    if len(rows) >= cap:
        # attr key is `preflight`, not `kind`: `TelemetryEventLog.record(kind, …)`
        # reserves that name and a colliding attr is silently swallowed.
        return PreflightSkip("no_slot", {"preflight": KIND_SLOT_CAP, "open_rows": len(rows),
                                         "max_open_rows": cap})
    return None


_KINDS = {KIND_SLOT_CAP: _slot_cap}


def preflight_skip(job: Any, *, data_dir: str) -> Optional[PreflightSkip]:
    """A :class:`PreflightSkip` when the job's declared precondition is already
    false, else None (run the tick). Never raises."""
    try:
        payload = dict(getattr(job, "payload", None) or {})
        if payload.get("deliver"):
            return None
        # X evaluation 2026-09-26 (P2-1): a rail whose credential has an OPEN
        # verdict is a $0 tick for every job, declared preflight or not
        # (cron/credential_preflight.py, one table kind -> tools).
        from cron.credential_preflight import credential_skip
        cred = credential_skip(job, payload, data_dir=data_dir)
        if cred is not None:
            return cred
        spec = payload.get("preflight")
        if not isinstance(spec, dict):
            return None
        fn = _KINDS.get(str(spec.get("kind") or ""))
        if fn is None:
            return None
        return fn(spec, data_dir=data_dir)
    except Exception:
        logger.warning("preflight error — failing open (running the tick)", exc_info=True)
        return None
