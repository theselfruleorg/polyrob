"""The in-process hand-off from ``tx_guard.authorize`` to the signing point.

Every EVM money verb in this tree runs ``tx_guard.authorize(intent, tx)`` and
then ``EvmRail.sign_and_send(tx)`` — the rail never sees the intent. So
``authorize`` records here which intent it allowed each transaction under, and
the signing point takes it back by the transaction's digest: the rail itself in
``local`` mode, the shadow comparison, or the remote signer (which re-runs the
guard on the SAME intent).

Recorded in EVERY ``WALLET_SIGNER`` mode (125 S3) for an ALLOWED decision, and
never for a dry run (``authorize(..., dry_run=True)``). The owner-approval lane
(``owner_queue``) is an ask, not an authorization (audit 2026-11-21 L-7):
``authorize`` records it only under ``remote``, where the signer re-runs the guard
and asks the owner on its own box. In ``local``/``shadow`` mode the verb records
it with :func:`record_owner_cleared` — only where it has proven the owner's
approval, for that exact transaction, right before ``sign_and_send``. The signer's deposit sweep records an
explicit marker intent (``server.DEPOSIT_SWEEP_INTENT``) for its own rail.

A transaction with no recorded intent is refused by ``EvmRail.sign_and_send``
in every mode, before any RPC or signature: it reached the signing point
without passing ``tx_guard`` in this process.

Bounded (64 entries, 15 minutes) and taken ONCE — a signed transaction cannot
be replayed through the same authorization.
"""
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

_MAX = 64
_TTL_SEC = 900.0
_lock = threading.Lock()
_items: "OrderedDict[str, tuple]" = OrderedDict()


def _digest(tx: Dict[str, Any]) -> str:
    from core.signer.protocol import tx_digest
    return tx_digest(tx)


def record(intent: Any, tx: Dict[str, Any], *, clock=time.monotonic) -> None:
    try:
        key = _digest(tx)
    except Exception:
        return
    now = clock()
    with _lock:
        _items[key] = (intent, now)
        _items.move_to_end(key)
        while len(_items) > _MAX:
            _items.popitem(last=False)


def record_owner_cleared(intent: Any, tx: Dict[str, Any]) -> None:
    """The ONE step a verb takes after it proved the owner approved an
    ``owner_queue`` decision: record *intent* for this exact *tx* (its digest), so
    the rail signs it once. Call it after ``size_gas``, right before
    ``sign_and_send``, and never on a path the owner did not clear."""
    if intent is None:
        raise ValueError("an owner-cleared transaction needs the intent it was asked under")
    record(intent, tx)


def peek(tx: Dict[str, Any], *, clock=time.monotonic) -> Optional[Any]:
    """The intent *tx* was authorized under, without consuming it (shadow)."""
    return _get(tx, pop=False, clock=clock)


def take(tx: Dict[str, Any], *, clock=time.monotonic) -> Optional[Any]:
    """The intent *tx* was authorized under, consumed (remote send)."""
    return _get(tx, pop=True, clock=clock)


def _get(tx, *, pop: bool, clock) -> Optional[Any]:
    try:
        key = _digest(tx)
    except Exception:
        return None
    now = clock()
    with _lock:
        hit = _items.get(key)
        if hit is None:
            return None
        intent, ts = hit
        if now - ts > _TTL_SEC:
            _items.pop(key, None)
            return None
        if pop:
            _items.pop(key, None)
        return intent


def _reset_for_tests() -> None:
    with _lock:
        _items.clear()
