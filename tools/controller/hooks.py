"""Tool-call hook pipeline (Item 7E/7H — extracted from ``controller/service.py``).

Owns the three Reference-style hook lists and the fail-mode execution engine that the
``Controller`` previously inlined:

  - **pre** ``(action_name, params, context) -> Optional[str]`` — a non-empty string
    DENIES the action with that reason; runs before ``act()``.
  - **transform** ``(action_name, params, result, context) -> Optional[ActionResult]``
    — a returned result REPLACES the current one; hooks chain in order.
  - **post** ``(action_name, params, result, context) -> None`` — observe-only
    (billing/metrics/audit); return value ignored.

Each hook registers with ``fail_mode``: ``"open"`` (default, legacy) swallows a
raising hook and continues; ``"closed"`` (guardrail/billing) turns a crash into a
DENY (pre) / error result (transform) / propagation (post). Every hook exception is
logged at ERROR as a ``hook.error`` telemetry signal in both modes.

The ``Controller`` keeps its public ``register_*`` / ``_run_*`` methods as thin
delegators to an instance of this class (its ``_pre_tool_call_hooks`` etc. attributes
proxy to ``pre``/``post``/``transform`` here), so existing callers and tests are
unchanged.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, List, Optional, Tuple

from tools.controller.types import ActionResult

# A registered hook entry is normalised to ``(callable, fail_mode)``.
HookEntry = Tuple[Callable, str]


class HookPipeline:
    """Stateful owner of the pre/post/transform hook lists + fail-mode engine."""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.pre: List = []
        self.post: List = []
        self.transform: List = []

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def normalize(entry: Any) -> HookEntry:
        """Accept either a bare callable or a ``(callable, fail_mode)`` tuple."""
        if isinstance(entry, tuple):
            hook, fail_mode = entry
            return hook, (fail_mode or "open")
        return entry, "open"

    def _log_error(self, phase: str, action_name: str, fail_mode: str, exc: Exception) -> None:
        """Structured telemetry signal for a hook exception (both fail modes)."""
        self.logger.error(
            f"hook.error phase={phase} action={action_name} fail_mode={fail_mode} "
            f"exc={type(exc).__name__}: {exc}"
        )

    async def _maybe_await(self, hook: Callable, *args: Any) -> Any:
        """Call a hook that may be sync or async; always return its resolved value.

        A sync hook returns its value directly (no thread offload — hooks are
        O(microseconds): set membership, metric increments, result rewrites). An
        async hook (e.g. an interactive/remote approval provider) is ``await``ed
        cooperatively so it yields the loop instead of freezing it. A hook that
        raises does so from ``hook(*args)`` (sync) or ``await res`` (async); either
        way the exception propagates to the caller's ``try/except`` unchanged. A
        sync ``def`` that *returns* a coroutine is also awaited (``isawaitable``).
        """
        res = hook(*args)
        if inspect.isawaitable(res):
            return await res
        return res

    # -- registration ---------------------------------------------------------

    def register_pre(self, hook: Callable, fail_mode: str = "open") -> None:
        self.pre.append((hook, fail_mode))

    def register_post(self, hook: Callable, fail_mode: str = "open") -> None:
        self.post.append((hook, fail_mode))

    def register_transform(self, hook: Callable, fail_mode: str = "open") -> None:
        self.transform.append((hook, fail_mode))

    # -- execution ------------------------------------------------------------

    async def run_pre(self, action_name, params, context) -> Optional[str]:
        """Run pre hooks; return the first denial reason, or None to allow.

        Async (UP-04): each hook is awaited via ``_maybe_await`` so a slow async
        hook yields the loop instead of blocking it; sync hooks run unchanged.
        """
        for entry in self.pre or []:
            hook, fail_mode = self.normalize(entry)
            try:
                reason = await self._maybe_await(hook, action_name, params, context)
            except Exception as e:
                self._log_error("pre", action_name, fail_mode, e)
                if fail_mode == "closed":
                    return f"guardrail pre-hook error: {type(e).__name__}: {e}"
                continue
            if reason:
                return reason
        return None

    async def run_transform(self, action_name, params, result, context):
        """Run transform hooks in order, chaining replacements."""
        for entry in self.transform or []:
            hook, fail_mode = self.normalize(entry)
            try:
                replacement = await self._maybe_await(hook, action_name, params, result, context)
            except Exception as e:
                self._log_error("transform", action_name, fail_mode, e)
                if fail_mode == "closed":
                    return ActionResult(
                        error=f"transform hook error on '{action_name}': {type(e).__name__}: {e}"
                    )
                continue
            if replacement is not None:
                result = replacement
        return result

    async def run_post(self, action_name, params, result, context) -> None:
        """Run post hooks. fail_mode=open swallows; closed re-raises."""
        for entry in self.post or []:
            hook, fail_mode = self.normalize(entry)
            try:
                await self._maybe_await(hook, action_name, params, result, context)
            except Exception as e:
                self._log_error("post", action_name, fail_mode, e)
                if fail_mode == "closed":
                    raise
