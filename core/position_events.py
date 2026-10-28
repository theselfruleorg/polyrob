"""Position lifecycle events (086 W1, §3.3) — core tier.

The position book (``core/open_positions.py``) and the retire verb
(``core/position_retire.py``) fire ONE event after each committed change to a
row, so a listener (the trading lifecycle, ``polyrob/app/trading_lifecycle.py``,
W4) can arm or disarm the row's exit job without polling the book.

Kinds:

* ``opened``      — a new row, a row restarted after a retire, a dust row a buy
  reopened, or an inherited row (``reason="inherited"``);
* ``changed``     — a partial sell, a buy into an open row, or a status change
  that keeps the row active (quarantined, re-opened);
* ``closed``      — a full exit DELETED the row (``reason`` = the delta's close
  reason when the seller named one: ``stop_loss`` · ``take_profit`` · …);
* ``written_off`` · ``dust`` · ``retired`` — the status changes of the same names.

``qty`` is the size held AFTER the change (0 on ``closed``); ``usd`` is the USD of
the leg that caused it (an acquire's cost, a dispose's proceeds; ``None`` =
unknown or no leg).

Rules (the shape of ``core.autonomy_control.register_transition_hook``):
process-local, synchronous, many listeners, fired AFTER the write committed. A
listener that raises is logged and skipped — an event never breaks the book
write it reports, and one listener never stops the next.

The telemetry listener (``position_opened`` / ``position_closed`` rows) lives in
``core/event_log.py`` and registers there at import: this module imports nothing
(an edge from here into the event log closed the large core import cycle,
``tests/test_layering_ratchet.py``). Tier-0.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

OPENED = "opened"
CHANGED = "changed"
CLOSED = "closed"
WRITTEN_OFF = "written_off"
RETIRED = "retired"
DUST = "dust"
KINDS = (OPENED, CHANGED, CLOSED, WRITTEN_OFF, RETIRED, DUST)
#: The kinds after which the row holds nothing the rail still manages.
TERMINAL_KINDS = (CLOSED, WRITTEN_OFF, RETIRED, DUST)


@dataclass(frozen=True)
class PositionEvent:
    kind: str
    user_id: str
    chain: str
    #: ``""`` = the treasury; else the token-bound account (``open_positions``).
    account: str
    address: str
    symbol: Optional[str] = None
    qty: float = 0.0
    usd: Optional[float] = None
    reason: str = ""
    ts: float = 0.0

    def to_attrs(self) -> dict:
        d = asdict(self)
        d.pop("user_id", None)
        return d


Listener = Callable[[PositionEvent], None]
_LISTENERS: List[Listener] = []


def register_listener(fn: Listener) -> None:
    """Process-local: ``fn(event)`` runs synchronously after every committed change."""
    if fn not in _LISTENERS:
        _LISTENERS.append(fn)


def unregister_listener(fn: Listener) -> None:
    try:
        _LISTENERS.remove(fn)
    except ValueError:
        pass


def fire(event: PositionEvent) -> None:
    """Call every listener. Never raises."""
    for fn in list(_LISTENERS):
        try:
            fn(event)
        except Exception:
            logger.warning("position_events: listener %r failed on %s", fn, event.kind,
                           exc_info=True)


def emit(kind: str, *, user_id: str, chain: str, account: str, address: str,
         symbol: Optional[str] = None, qty: float = 0.0, usd: Optional[float] = None,
         reason: str = "", ts: Optional[float] = None) -> None:
    """Build one event and :func:`fire` it. Never raises (the book's call site)."""
    try:
        fire(PositionEvent(kind=kind, user_id=str(user_id or ""), chain=str(chain or ""),
                           account=str(account or ""), address=str(address or ""),
                           symbol=symbol, qty=float(qty or 0.0),
                           usd=None if usd is None else float(usd),
                           reason=str(reason or "")[:300],
                           ts=time.time() if ts is None else float(ts)))
    except Exception:
        logger.debug("position_events: event not built", exc_info=True)


__all__ = ["CHANGED", "CLOSED", "DUST", "KINDS", "OPENED", "PositionEvent", "RETIRED",
           "TERMINAL_KINDS", "WRITTEN_OFF", "emit", "fire", "register_listener",
           "unregister_listener"]
