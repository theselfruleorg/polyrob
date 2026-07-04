"""Session + subagent lifecycle hooks (C-N1 — Reference-parity).

Extends POLYROB's hook surface from tool-call only (Controller pre/post/transform) to
session and delegation lifecycle. The orchestrator fires ``on_session_start`` /
``on_session_end``; the delegation path fires ``on_subagent_start`` /
``on_subagent_end``. These unlock clean billing / audit / memory taps without
threading callbacks through the agent core.

Every hook is observe-only and fail-open — a raising hook is logged and ignored,
never breaking the session lifecycle. Hooks may be sync or async; a coroutine return
is awaited. Each hook receives a single ``event`` dict.

Mirrors the registration ergonomics of ``tools/controller/service.py`` pre/post/
transform hooks so the whole hook surface feels consistent.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Dict, List, Optional

_module_logger = logging.getLogger(__name__)

Hook = Callable[[Dict[str, Any]], Any]


class SessionHooksMixin:
    """Session/subagent lifecycle hook registry + fail-open async runners."""

    def _hook_logger(self) -> logging.Logger:
        return getattr(self, "logger", None) or _module_logger

    @staticmethod
    def _append(holder: Optional[List[Hook]], hook: Hook) -> List[Hook]:
        if holder is None:
            holder = []
        holder.append(hook)
        return holder

    # ---- registration ----------------------------------------------------

    def register_session_start_hook(self, hook: Hook) -> None:
        """Fired once when a session is created. ``event`` carries session metadata."""
        self._session_start_hooks = self._append(
            getattr(self, "_session_start_hooks", None), hook)

    def register_session_end_hook(self, hook: Hook) -> None:
        """Fired once when a session is cleaned up."""
        self._session_end_hooks = self._append(
            getattr(self, "_session_end_hooks", None), hook)

    def register_subagent_start_hook(self, hook: Hook) -> None:
        """Fired before a delegated subtask runs."""
        self._subagent_start_hooks = self._append(
            getattr(self, "_subagent_start_hooks", None), hook)

    def register_subagent_end_hook(self, hook: Hook) -> None:
        """Fired after a delegated subtask completes (``event['ok']`` = success)."""
        self._subagent_end_hooks = self._append(
            getattr(self, "_subagent_end_hooks", None), hook)

    # ---- runners (fail-open; sync or async hooks) ------------------------

    async def _run_lifecycle_hooks(self, attr: str, label: str, event: Dict[str, Any]) -> None:
        for hook in getattr(self, attr, None) or []:
            try:
                result = hook(event)
                if inspect.iscoroutine(result):
                    await result
            except Exception as e:  # observe-only seam must never break the lifecycle
                self._hook_logger().warning(f"{label} hook raised, ignoring: {e}")

    async def _run_session_start_hooks(self, **event: Any) -> None:
        await self._run_lifecycle_hooks("_session_start_hooks", "session_start", event)

    async def _run_session_end_hooks(self, **event: Any) -> None:
        await self._run_lifecycle_hooks("_session_end_hooks", "session_end", event)

    async def _run_subagent_start_hooks(self, **event: Any) -> None:
        await self._run_lifecycle_hooks("_subagent_start_hooks", "subagent_start", event)

    async def _run_subagent_end_hooks(self, **event: Any) -> None:
        await self._run_lifecycle_hooks("_subagent_end_hooks", "subagent_end", event)
