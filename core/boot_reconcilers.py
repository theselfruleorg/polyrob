"""Cold-start reconcilers the autonomy runtime runs once at boot.

``core`` may not import ``tools`` (``tests/test_layering_ratchet.py``). A tool
that needs a one-shot reconcile at cold start (``hf_deploy`` re-checks its
``live`` Spaces) registers a coroutine function here at import;
``core/autonomy_runtime.py`` looks it up by tool id. Same seam shape as
``core/wallet/bridge_status.py``. No registration means no sweep.
"""
from typing import Awaitable, Callable, Dict, Optional

#: ``() -> awaitable count of rows the sweep changed``.
Reconciler = Callable[[], Awaitable[int]]

_RECONCILERS: Dict[str, Reconciler] = {}


def register_boot_reconciler(tool_id: str, fn: Optional[Reconciler]) -> None:
    """Last registration wins; None restores an absent hook on failed loading."""
    if fn is None:
        _RECONCILERS.pop(tool_id, None)
    elif not callable(fn):
        raise TypeError("boot reconciler must be callable")
    else:
        _RECONCILERS[tool_id] = fn


def boot_reconciler(tool_id: str) -> Optional[Reconciler]:
    """The registered reconciler for ``tool_id``, or ``None``."""
    return _RECONCILERS.get(tool_id)


__all__ = ["Reconciler", "register_boot_reconciler", "boot_reconciler"]
