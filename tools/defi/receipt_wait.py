"""The ONE receipt wait for a broadcast money verb: a cancel never loses the row.

CLI1 (2026-10-03 interface audit): a Ctrl-C or task cancel while a money verb
waited for its receipt unwound past ``gate.record`` — the transaction was
already broadcast, but it was missing from the trailing-24h cap and from
``/book``. Every EVM money verb that waits for a receipt BEFORE it records
goes through this module, so the broadcast is recorded before the cancel
propagates and the owner is told the tx may have gone through.

* :func:`await_receipt_or_record` — the receipt wait itself;
* :func:`record_on_interrupt` — the same protection over any other awaited
  window between the broadcast and the record (a receipt read-back, a paid-fee
  read). Set ``handle.recorded = True`` once the verb has recorded.

The record here is synchronous so it lands before the cancel propagates. The
exception is always re-raised: this module never swallows a cancel.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import contextmanager
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

INTERRUPTED = "interrupted — the tx may have gone through"


class InterruptHandle:
    """Carried through :func:`record_on_interrupt`; ``recorded`` stops a double row."""

    __slots__ = ("recorded",)

    def __init__(self) -> None:
        self.recorded = False


def record_interrupted(tx_hash: str, *, gate, record_kw: Dict[str, Any],
                       tool=None, execution_context=None,
                       notice_kw: Optional[Dict[str, Any]] = None) -> None:
    """Record the broadcast, say so on stderr and to the owner. Never raises."""
    try:
        gate.record(**record_kw)
    except Exception:
        logger.error("money verb: interrupted broadcast %s NOT recorded", tx_hash,
                     exc_info=True)
    line = (f"{INTERRUPTED}: {tx_hash}. It is recorded against the daily cap; "
            f"check /book before you retry.")
    logger.warning("money verb: %s", line)
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    notify = getattr(tool, "_notify_tx", None) if notice_kw is not None else None
    if notify is None:
        return
    try:
        from core.wallet import tx_notify
        kw = dict(notice_kw)
        notify(execution_context, tx_notify.TxNotice(
            chain=kw.pop("chain", None), tx_ref=tx_hash,
            state=tx_notify.STATE_IN_FLIGHT, detail=INTERRUPTED,
            ledger_recorded=True, **kw), settled=True)
    except Exception:
        logger.debug("money verb: interrupted notice skipped", exc_info=True)


@contextmanager
def record_on_interrupt(tx_hash: str, *, gate, record_kw: Dict[str, Any], tool=None,
                        execution_context=None,
                        notice_kw: Optional[Dict[str, Any]] = None):
    """Record the broadcast if a cancel / Ctrl-C unwinds this block, then re-raise."""
    handle = InterruptHandle()
    try:
        yield handle
    except (asyncio.CancelledError, KeyboardInterrupt):
        if not handle.recorded:
            record_interrupted(tx_hash, gate=gate, record_kw=record_kw, tool=tool,
                               execution_context=execution_context, notice_kw=notice_kw)
        raise


async def await_receipt_or_record(rail, tx_hash: str, *, gate, record_kw: Dict[str, Any],
                                  tool=None, execution_context=None,
                                  notice_kw: Optional[Dict[str, Any]] = None):
    """``rail.await_receipt`` off the loop; on a cancel, record the broadcast first."""
    with record_on_interrupt(tx_hash, gate=gate, record_kw=record_kw, tool=tool,
                             execution_context=execution_context, notice_kw=notice_kw):
        return await asyncio.to_thread(rail.await_receipt, tx_hash)
