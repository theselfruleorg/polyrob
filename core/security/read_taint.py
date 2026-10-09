"""Per-turn read taint: the turn has read third-party content (AGT-1 / DATA-1).

Every owner-authority gate asks "is this a genuine owner turn?" Nothing recorded
that the turn had since read text a third party wrote — a web page, a mail, an
X post, MCP or x402 output, a file, a sub-agent report, a recall. The model
chooses tool arguments, and that text can choose them too. So a turn that has
read such content keeps the owner's ACCESS (it may still create goals and cron
jobs), but loses the owner's AUTHORSHIP: what it writes is agent-authored
(restrict-only), and the persistent-authority verbs that only an owner may
decide refuse.

Set by the Controller after an action whose result is untrusted
(``is_untrusted_tool`` — the per-tool ``untrusted_output`` rows — or a result
that carries the ``<untrusted_tool_result>`` frame). Stored on the orchestrator
(it survives the steps of a turn) and stamped into each step's execution
context (``metadata["untrusted_read"]``). A genuine owner message drained into
the session clears it (``agents/task/agent/core/user_ingress.py``): the owner is
speaking again. Fail toward tainted: an unreadable marker reads as tainted only
where the gate already holds an orchestrator.
"""
from __future__ import annotations

from typing import Any, Optional

#: The orchestrator attribute, and the execution-context metadata key.
ATTR = "_untrusted_read"
META_KEY = "untrusted_read"
_FRAME = "<untrusted_tool_result"


def result_is_untrusted(action_name: Optional[str], tool: Optional[str], result: Any) -> bool:
    """True when this action's result carries third-party bytes."""
    try:
        from core.security.untrusted_wrap import is_untrusted_tool
        if is_untrusted_tool(action_name, tool):
            return True
    except Exception:
        return True
    for field in ("extracted_content", "error"):
        text = getattr(result, field, None)
        if isinstance(text, str) and _FRAME in text:
            return True
    return False


def note_result(orchestrator: Any, execution_context: Any, action_name: Optional[str],
                tool: Optional[str], result: Any) -> None:
    """Mark the turn tainted when this result is untrusted. Never raises."""
    try:
        if not result_is_untrusted(action_name, tool, result):
            return
        mark(orchestrator, execution_context)
    except Exception:
        pass


def mark(orchestrator: Any, execution_context: Any = None) -> None:
    if orchestrator is not None:
        try:
            setattr(orchestrator, ATTR, True)
        except Exception:
            pass
    meta = getattr(execution_context, "metadata", None)
    if isinstance(meta, dict):
        meta[META_KEY] = True


def clear(orchestrator: Any) -> None:
    if orchestrator is not None:
        try:
            setattr(orchestrator, ATTR, False)
        except Exception:
            pass


def orchestrator_tainted(orchestrator: Any) -> bool:
    return getattr(orchestrator, ATTR, False) is True


def is_tainted(execution_context: Any) -> bool:
    """True when this turn has read untrusted content (per the step's context)."""
    meta = getattr(execution_context, "metadata", None)
    return bool(isinstance(meta, dict) and meta.get(META_KEY))
