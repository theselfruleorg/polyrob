"""Cron preflight — a $0 skip when a DETERMINISTIC precondition is already
false before any model call.

The one check today: a job whose declared tool has a live credential verdict
(``cron/credential_preflight.py``). The tick is then a ``cron_run skipped``
event instead of a paid turn.

Fail-OPEN by construction: any error runs the tick. Delivery jobs are never
preflighted (their run IS the delivery).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightSkip:
    reason: str
    attrs: Dict[str, Any] = field(default_factory=dict)
    #: an owner-facing notice the runner delivers with this skip (the credential
    #: preflight sets it on the FIRST skip of a verdict episode only)
    notice: Optional[str] = None


def preflight_skip(job: Any, *, data_dir: str) -> Optional[PreflightSkip]:
    """A :class:`PreflightSkip` when the job cannot do its work this tick, else
    None (run the tick). Never raises."""
    try:
        payload = dict(getattr(job, "payload", None) or {})
        if payload.get("deliver"):
            return None
        from cron.credential_preflight import credential_skip
        return credential_skip(job, payload, data_dir=data_dir)
    except Exception:
        logger.warning("preflight error — failing open (running the tick)", exc_info=True)
        return None
