"""The bridge status-reader seam (039 Unit C).

`core` may not import `tools` (`tests/test_layering_ratchet.py`), and the bridge
watcher lives in core because it is a wallet reconciliation loop. Relay's status
endpoint lives in `tools/defi/providers/relay_bridge.py` because it is a provider.
This is the seam between them, and it mirrors `MemoryProviderRegistry`: the tool
tier registers an implementation, core consults whatever is registered.

⚠️ The registered reader is the LESSER half of the guard on purpose. The arrival
is proven by the MEASURED destination balance, which core reads itself and which
needs no provider at all. A status only distinguishes "not yet" from "refunded".
So an unregistered reader degrades the watcher honestly — it can still settle an
arrival and still escalate a stuck bridge; it simply never declares a failure,
which is the conservative direction. It must never be the other way round.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

#: ``(request_id) -> (state, detail)`` where state is
#: ``success|pending|failure|unknown``.
StatusReader = Callable[[str], Tuple[str, str]]

_READER: Optional[StatusReader] = None


def register_status_reader(reader: StatusReader) -> None:
    """Called from the tool tier at import. Last registration wins."""
    global _READER
    _READER = reader


def get_status_reader() -> Optional[StatusReader]:
    return _READER


def read_status(request_id: str) -> Tuple[str, str]:
    """``(state, detail)``. ``unknown`` when no reader is registered or it raises.

    `unknown` is a first-class answer here, exactly as it is in the provider: a
    status that did not resolve must never be reported as arrival or as loss.
    """
    reader = _READER
    if reader is None:
        return "unknown", "no bridge status reader is registered"
    try:
        return reader(str(request_id))
    except Exception as exc:
        logger.debug("bridge status read failed", exc_info=True)
        return "unknown", f"status read failed: {type(exc).__name__}: {exc}"
