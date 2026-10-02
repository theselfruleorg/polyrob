"""The in-process hand-off from ``tx_guard.authorize`` to the signing point.

Every EVM money verb in this tree runs ``tx_guard.authorize(intent, tx)`` and
then ``EvmRail.sign_and_send(tx)`` — the rail never sees the intent. The signer
must re-run the guard on the SAME intent, so ``authorize`` records here which
intent it allowed each transaction under, and the signing point (the shadow or
remote signer) takes it back by the transaction's digest.

Recorded only when ``WALLET_SIGNER`` is not ``local`` (zero change there), and
only for an ALLOWED decision or the owner-approval lane (``owner_queue``: the
verb asks the owner and re-authorizes; the signer applies its own hard cap).

A transaction with no recorded intent is refused by the remote signer: it
reached the signing point without passing ``tx_guard`` in this process.

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
