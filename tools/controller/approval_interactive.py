"""Interactive CLI approval provider (P0-F).

Wires the (mechanism-only) ApprovalProvider seam to an actual owner prompt. On a gated
action the provider prints the action + a params digest and asks the operator to approve
(y/yes) or deny (anything else) on the terminal. The blocking input runs in a worker
thread (``asyncio.to_thread``) so it yields the event loop; the whole call is bounded by
the approval hook's ``asyncio.wait_for``. Injectable ``input_fn`` makes it unit-testable
without a TTY.

⚠️ Single stdin reader (H8). A blocking ``input()`` in a worker thread cannot be
interrupted by an ``asyncio.wait_for`` timeout — cancelling the *await* leaves the thread
reading stdin. If a second gated action then prompted, it would spawn a SECOND stdin
reader and a late keystroke could be consumed by the wrong (stale, already-timed-out)
prompt. So only ONE interactive prompt owns stdin at a time: a second concurrent prompt
denies (fail-closed) instead of racing. The in-flight flag is cleared by the reader
thread itself when ``input()`` finally returns (even after the await was cancelled), so a
timed-out prompt's lingering thread can never be mistaken for a free stdin.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Dict, Optional

from tools.controller.approval import ApprovalProvider, register_approval_provider

logger = logging.getLogger(__name__)

# Set while a real blocking input() is outstanding on stdin (process-wide — there is
# only one stdin). Cleared by the reader thread's own finally, not on await-cancel.
_stdin_in_flight = threading.Event()


class InteractiveCLIApprover(ApprovalProvider):
    """Prompt the local operator to approve/deny a gated action."""

    def __init__(self, input_fn: Optional[Callable[[str], str]] = None) -> None:
        self._input_fn = input_fn or input  # real stdin by default; injectable for tests

    @staticmethod
    def _digest(params: Dict[str, Any]) -> str:
        try:
            items = list((params or {}).items())
        except Exception:
            return "(unprintable params)"
        parts = [f"{k}={str(v)[:60]}" for k, v in items[:6]]
        return "{" + ", ".join(parts) + "}"

    def _prompt(self, action_name: str, params: Dict[str, Any]) -> str:
        return f"\n[approval] Allow '{action_name}'? {self._digest(params)}\n  approve? [y/N]: "

    async def request(self, action_name: str, params: Dict[str, Any], context: Any) -> bool:
        # H8: only one interactive prompt may own stdin at a time. If a prompt is
        # already outstanding, deny (fail-closed) rather than spawn a competing reader.
        if _stdin_in_flight.is_set():
            logger.warning(
                "approval prompt already in flight -> deny '%s' (single stdin reader)",
                action_name,
            )
            return False

        prompt = self._prompt(action_name, params)
        input_fn = self._input_fn

        def _blocking_read() -> str:
            # The finally runs in THIS worker thread when input() actually returns, so
            # the in-flight flag stays set until the operator responds — even if the
            # awaiting coroutine was already cancelled by a wait_for timeout.
            try:
                return input_fn(prompt)
            finally:
                _stdin_in_flight.clear()

        _stdin_in_flight.set()
        try:
            answer = await asyncio.to_thread(_blocking_read)
        except asyncio.CancelledError:
            # Timeout/cancel: the reader thread keeps running and will clear the flag
            # when input() returns; until then new prompts deny. Propagate -> deny.
            logger.info("approval prompt cancelled for '%s' -> deny", action_name)
            raise
        except Exception as e:
            logger.error("approval prompt error for '%s': %s -> deny", action_name, e)
            return False
        return str(answer).strip().lower() in ("y", "yes")


# Register under the APPROVAL_PROVIDER name 'interactive_cli'.
register_approval_provider("interactive_cli", InteractiveCLIApprover)
