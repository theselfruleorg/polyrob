"""live_hooks.py — bridge the orchestrator's lifecycle hooks into SessionState.

The orchestrator fires sub-agent start/end hooks (``_run_subagent_start_hooks`` /
``_run_subagent_end_hooks`` in ``agents/task/session/hooks.py``, fired from
``sub_agent_manager.py``) but nothing in the CLI subscribed — so a ``delegate_task``
spawn was invisible. ``make_subagent_hooks`` builds the two fail-open callbacks the
REPL registers on the orchestrator; they bump the live sub-agent counter on
``SessionState`` (which the status line reads).

Hooks are called by the orchestrator with keyword args (``goal``, ``agent_id``,
``parent_session_id`` and, on end, ``ok``). The callbacks accept arbitrary kwargs
and never raise — a UI-counter hook must never break the agent loop.
"""

from __future__ import annotations

from typing import Any, Callable, Tuple

from cli.ui.state import SessionState

Hook = Callable[..., None]


def make_subagent_hooks(state: SessionState) -> Tuple[Hook, Hook]:
    """Return ``(start_hook, end_hook)`` that keep ``state.subagents_active`` live.

    Both are fail-open (swallow any error) and accept arbitrary kwargs so a
    change to the orchestrator's hook signature can't crash the REPL.
    """

    def _start(**_event: Any) -> None:
        try:
            state.note_subagent_start()
        except Exception:  # pragma: no cover - a UI counter must never break the loop
            pass

    def _end(**_event: Any) -> None:
        try:
            state.note_subagent_end()
        except Exception:  # pragma: no cover
            pass

    return _start, _end
