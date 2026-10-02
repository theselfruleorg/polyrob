"""Console route contributions (067 P5a).

The console has five destinations (``pages_new.NAV``) and the nav ratchet keeps
it at five: a pack never adds a page. What a pack CAN add is the data a
destination draws — a FastAPI router of JSON readers/writers — registered here
against the destination it serves and mounted by ``pages_new.mount`` beside the
console's own API routers (same clash rule: a path the app already serves keeps
its existing route).

A destination with no contribution still renders; its page shows the install
hint instead of the panes (``provides(destination)`` is False). Today the Money
readers of ``webview/pages.py`` and ``webview/pages_new.py`` register from
their own modules; 067 P5b moves them into the wallet pack, which registers the
same routers from its own code.
"""
from __future__ import annotations

import importlib
from typing import Any, List, Tuple

#: The destinations a router may serve (``pages_new.NAV`` keys).
DESTINATIONS: Tuple[str, ...] = ("new", "inbox", "work", "money", "agent")

#: In-tree modules that register a contribution on import. P5b deletes the rows
#: as the wallet pack takes the Money readers over.
_IN_TREE: Tuple[str, ...] = ("webview.pages", "webview.pages_new", "webview.tokens_routes",
                             "webview.cards_routes", "webview.feed_routes",
                             "webview.workspace_routes")

_ROUTERS: List[Tuple[str, Any, str]] = []      # (destination, router, source)


def register_console_router(router: Any, *, destination: str, source: str) -> None:
    """Contribute ``router`` to ``destination``. Idempotent per router object.
    A destination outside :data:`DESTINATIONS`, or an object without
    ``routes``, is refused (``ValueError``)."""
    if destination not in DESTINATIONS:
        # Machine-shaped messages (the copy-layer ratchet reads this module).
        raise ValueError(f"unknown destination {destination!r} (from {source}); "
                         f"destinations={DESTINATIONS}")
    if not hasattr(router, "routes"):
        raise ValueError(f"not a router: {router!r} (from {source})")
    if any(r is router for _d, r, _s in _ROUTERS):
        return
    _ROUTERS.append((destination, router, str(source)))


def _load_in_tree() -> None:
    for name in _IN_TREE:
        importlib.import_module(name)


def contributed_routers(destination: str = "") -> tuple:
    """The contributed routers (of one destination, or all), in order."""
    _load_in_tree()
    return tuple(r for d, r, _s in _ROUTERS if not destination or d == destination)


def provides(destination: str) -> bool:
    """True when at least one router serves ``destination``."""
    return bool(contributed_routers(destination))


__all__ = ["DESTINATIONS", "contributed_routers", "provides", "register_console_router"]
