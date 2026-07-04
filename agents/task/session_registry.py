"""Session registry — maps session_id -> SessionOrchestrator.

This is the single seam through which the active in-memory orchestrators are
looked up and mutated. Today it wraps a plain in-process dict, which is what
forces ``UVICORN_WORKERS=1`` in production (an orchestrator created in one
worker is invisible to another). Keeping every access behind this interface
means the future swap to a cross-process, SQLite-backed registry (WAL mode,
jittered retry — see ``docs/ROB_CORE_SERVER_SPLIT_SPEC.md`` Phase E) is
mechanical rather than a scavenger hunt through ``task_agent_lite``.

The interface is intentionally small and dict-flavoured so callers read
naturally:

    registry.register(session_id, orchestrator)
    orch = registry.get(session_id)
    orch = registry.remove(session_id)      # pop, returns None if absent
    if session_id in registry: ...
    for sid, orch in registry.items(): ...
    len(registry)
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple


class SessionRegistry:
    """In-process registry of active session orchestrators.

    Not thread/process shared. See module docstring for the scaling caveat.
    """

    def __init__(self) -> None:
        self._orchestrators: Dict[str, Any] = {}

    def register(self, session_id: str, orchestrator: Any, *, params: Optional[Dict[str, Any]] = None) -> None:
        """Store (or replace) the orchestrator for ``session_id``.

        ``params`` is accepted (and ignored) for signature parity with the
        SQLite-backed registry, so callers can pass it without crashing whichever
        backend ``SESSION_REGISTRY_BACKEND`` selected.
        """
        self._orchestrators[session_id] = orchestrator

    def get(self, session_id: str) -> Optional[Any]:
        """Return the orchestrator for ``session_id`` or ``None``."""
        return self._orchestrators.get(session_id)

    def route(self, session_id: str):
        """Routing decision (P6). In-process registry knows only local sessions, so
        the result is either LOCAL (handle here) or MISSING."""
        from agents.task.session_route import SessionRoute, LOCAL, MISSING
        orch = self._orchestrators.get(session_id)
        if orch is not None:
            return SessionRoute(status=LOCAL, orchestrator=orch)
        return SessionRoute(status=MISSING)

    def remove(self, session_id: str) -> Optional[Any]:
        """Remove and return the orchestrator for ``session_id`` (``None`` if absent)."""
        return self._orchestrators.pop(session_id, None)

    def contains(self, session_id: str) -> bool:
        return session_id in self._orchestrators

    def count(self) -> int:
        return len(self._orchestrators)

    def items(self) -> List[Tuple[str, Any]]:
        """Snapshot of (session_id, orchestrator) pairs — safe to mutate while iterating."""
        return list(self._orchestrators.items())

    def values(self) -> List[Any]:
        """Snapshot of the registered orchestrators."""
        return list(self._orchestrators.values())

    def session_ids(self) -> List[str]:
        return list(self._orchestrators.keys())

    def clear(self) -> None:
        self._orchestrators.clear()

    # --- dict-flavoured conveniences ---

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._orchestrators

    def __len__(self) -> int:
        return len(self._orchestrators)

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._orchestrators.keys()))
