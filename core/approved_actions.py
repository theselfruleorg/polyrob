"""Late-approved actions the agent process runs from the durable wake drain.

A wake row whose ``metadata.kind`` names a registered runner is not a session
wake: it is ONE action the owner approved after the run that asked had stopped
waiting (an X post, a DM, a mail — ``tools.controller.approval_queue.
run_approved_outbound``). The runner lives in the tools tier and registers
itself here, so ``core.autonomy_runtime`` (the drain) never imports ``tools``.

A runner is ``async (metadata, user_id, task_agent) -> bool``: True when the row
is finished, False to retry it on a later tick.
"""
from typing import Any, Awaitable, Callable, Dict, Optional

Runner = Callable[[Dict[str, Any], str, Any], Awaitable[bool]]

_RUNNERS: Dict[str, Runner] = {}


def set_runner(kind: str, runner: Runner) -> None:
    _RUNNERS[str(kind)] = runner


def runner_for(metadata: Optional[Dict[str, Any]]) -> Optional[Runner]:
    if not isinstance(metadata, dict):
        return None
    return _RUNNERS.get(str(metadata.get("kind") or ""))


__all__ = ["runner_for", "set_runner"]
