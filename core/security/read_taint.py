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

The turn also records WHAT tainted it first (:func:`source`, e.g.
``"filesystem_read_file reports/promo-drafts.md"``) so a gate can name the real
cause. The successful receipt of the agent's own write (``_RECEIPT_ACTIONS``:
``filesystem_write_file`` & co.; a pack adds its own) is not third-party text and
does not taint; a file READ still does.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Mapping, Optional, Set, Tuple

logger = logging.getLogger(__name__)

#: The orchestrator attribute, and the execution-context metadata key.
ATTR = "_untrusted_read"
META_KEY = "untrusted_read"
#: WHAT tainted the turn first (``"<action> <path|url>"``), beside the mark, so a
#: gate can tell the agent and the owner the real cause (prod 2026-10-10: the
#: promo job's card said nothing about the drafts file that tainted the run).
SOURCE_ATTR = "_untrusted_read_source"
SOURCE_META_KEY = "untrusted_read_source"
_FRAME = "<untrusted_tool_result"
_SOURCE_MAX = 140
#: session id -> the turn's first source. The step loop builds each step's
#: context afresh with only the ``untrusted_read`` bool, so a gate that holds
#: just the context finds the cause here. Bounded; cleared with the taint.
_SESSION_SOURCES: "OrderedDict[str, str]" = OrderedDict()
_SESSION_SOURCES_MAX = 512
#: The params that name what an action read, in order of preference.
_SOURCE_PARAMS = ("file_path", "path", "url", "participant", "conversation_id", "user",
                  "query", "room")

#: ``(tool, action)`` pairs whose SUCCESSFUL result is a receipt of the agent's own
#: write — a path and a digest, a tweet id — never third-party bytes. Their tool
#: namespace stays untrusted for every other verb (a file READ still taints). A
#: failed receipt, or one that carries the untrusted frame, still taints. A pack
#: adds its own with :func:`register_receipt_actions` (layering: the hook lives
#: here, the X pack registers ``twitter_post`` & co. at load).
_RECEIPT_ACTIONS: Set[Tuple[str, str]] = {
    ("filesystem", f"filesystem_{verb}") for verb in (
        "write_file", "append_file", "create_directory", "delete_file", "copy_file",
        "jsonl_append", "jsonl_remove")
}


#: Deferred receipt sources: ``(tool, zero-arg callable)``. A pack whose list
#: derives from the verb-policy rows registers the CALLABLE, so its load builds
#: no policy view before every pack's rows are in (test_lazy_policy_views).
_PENDING_RECEIPTS: list = []


def register_receipt_actions(tool: str, actions: Any) -> None:
    """Declare *actions* of *tool* as write receipts (see ``_RECEIPT_ACTIONS``).
    *actions* is an iterable, or a zero-arg callable resolved on first use."""
    if callable(actions):
        _PENDING_RECEIPTS.append((tool, actions))
        return
    for action in actions or ():
        if tool and action:
            _RECEIPT_ACTIONS.add((str(tool), str(action)))


def _resolve_pending_receipts() -> None:
    while _PENDING_RECEIPTS:
        tool, source = _PENDING_RECEIPTS.pop(0)
        try:
            register_receipt_actions(tool, source())
        except Exception:
            logger.debug("receipt source for %s failed (its results still taint)", tool,
                         exc_info=True)


def _is_receipt(action_name: Optional[str], tool: Optional[str], result: Any) -> bool:
    _resolve_pending_receipts()
    if not action_name or not tool or (str(tool), str(action_name)) not in _RECEIPT_ACTIONS:
        return False
    return not getattr(result, "error", None)


def result_is_untrusted(action_name: Optional[str], tool: Optional[str], result: Any) -> bool:
    """True when this action's result carries third-party bytes."""
    for field in ("extracted_content", "error"):
        text = getattr(result, field, None)
        if isinstance(text, str) and _FRAME in text:
            return True
    if _is_receipt(action_name, tool, result):
        return False
    try:
        from core.security.untrusted_wrap import is_untrusted_tool
        if is_untrusted_tool(action_name, tool):
            return True
    except Exception:
        return True
    return False


def describe_source(action_name: Optional[str], params: Any = None) -> str:
    """``"<action> <what it read>"`` on one short line (params are model-chosen;
    newlines are folded and the line is capped)."""
    what = ""
    try:
        if params is not None and not isinstance(params, Mapping):
            params = params.model_dump() if hasattr(params, "model_dump") else vars(params)
        for key in _SOURCE_PARAMS:
            value = (params or {}).get(key)
            if value not in (None, ""):
                what = str(value)
                break
    except Exception:
        what = ""
    text = " ".join(f"{action_name or 'a tool'} {what}".split())
    return text if len(text) <= _SOURCE_MAX else text[:_SOURCE_MAX - 1] + "…"


def note_result(orchestrator: Any, execution_context: Any, action_name: Optional[str],
                tool: Optional[str], result: Any, params: Any = None) -> None:
    """Mark the turn tainted when this result is untrusted. Never raises."""
    try:
        if not result_is_untrusted(action_name, tool, result):
            return
        mark(orchestrator, execution_context,
             source=describe_source(action_name, params))
    except Exception:
        pass


def enabled() -> bool:
    """The owner's ``SECURITY_SETTINGS`` row ``read_taint`` (D1, 2026-10-10):
    ``on`` (default) or ``off`` — off, the taint is never set and never read."""
    try:
        from core.security import settings
        return settings.value("read_taint") != "off"
    except Exception:
        return True


def mark(orchestrator: Any, execution_context: Any = None,
         source: Optional[str] = None) -> None:
    """Taint the turn. The FIRST source of the turn is kept (it is the cause)."""
    if not enabled():
        return
    if orchestrator is not None:
        try:
            if source and not (getattr(orchestrator, ATTR, False) is True
                               and getattr(orchestrator, SOURCE_ATTR, None)):
                setattr(orchestrator, SOURCE_ATTR, source)
            setattr(orchestrator, ATTR, True)
        except Exception:
            pass
    meta = getattr(execution_context, "metadata", None)
    if isinstance(meta, dict):
        if source and not (meta.get(META_KEY) and meta.get(SOURCE_META_KEY)):
            inherited = getattr(orchestrator, SOURCE_ATTR, None) if orchestrator else None
            meta[SOURCE_META_KEY] = inherited or source
        meta[META_KEY] = True
    sid = _sid(orchestrator, execution_context)
    if source and sid and sid not in _SESSION_SOURCES:
        _SESSION_SOURCES[sid] = (getattr(orchestrator, SOURCE_ATTR, None)
                                 if orchestrator is not None else None) or source
        while len(_SESSION_SOURCES) > _SESSION_SOURCES_MAX:
            _SESSION_SOURCES.popitem(last=False)


def _sid(orchestrator: Any, execution_context: Any) -> Optional[str]:
    sid = getattr(execution_context, "session_id", None) or \
        getattr(orchestrator, "session_id", None)
    return str(sid) if isinstance(sid, str) and sid else None


def clear(orchestrator: Any) -> None:
    if orchestrator is not None:
        try:
            setattr(orchestrator, ATTR, False)
            setattr(orchestrator, SOURCE_ATTR, None)
        except Exception:
            pass
        sid = _sid(orchestrator, None)
        if sid:
            _SESSION_SOURCES.pop(sid, None)


def source(*, orchestrator: Any = None, execution_context: Any = None) -> Optional[str]:
    """What tainted this turn first, or None (untainted, or a legacy mark)."""
    meta = getattr(execution_context, "metadata", None)
    if isinstance(meta, dict) and meta.get(META_KEY):
        if meta.get(SOURCE_META_KEY):
            return str(meta[SOURCE_META_KEY])
        sid = _sid(None, execution_context)
        if sid and _SESSION_SOURCES.get(sid):
            return _SESSION_SOURCES[sid]
    if orchestrator is not None and getattr(orchestrator, ATTR, False) is True:
        value = getattr(orchestrator, SOURCE_ATTR, None)
        return str(value) if value else None
    return None


def orchestrator_tainted(orchestrator: Any) -> bool:
    return enabled() and getattr(orchestrator, ATTR, False) is True


def is_tainted(execution_context: Any) -> bool:
    """True when this turn has read untrusted content (per the step's context)."""
    if not enabled():
        return False
    meta = getattr(execution_context, "metadata", None)
    return bool(isinstance(meta, dict) and meta.get(META_KEY))
