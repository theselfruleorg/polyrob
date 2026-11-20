"""`/self` and `/memory` on every owner seat (113 W6 G-6) — reads only.

The phone could not read the agent's identity documents or search its memory:
both verbs were terminal-only. These are the one text every seat renders — the
chat apps (``admin_verbs``), the console chat box and the terminal
(``cli/ui/commands/h_self.py``, ``handlers._h_memory``) — so one verb gives one
answer. Nothing here writes: identity docs change through the agent's
``self_context_manage`` action, memory scopes through ``polyrob owner memory``.
Both verbs are owner reads and are refused inside a group room (the verb row).
"""
from __future__ import annotations

from typing import Any, List, Optional

#: How much of each identity doc a reply shows before it cuts. The docs are
#: char-capped at load time already; this keeps one reply readable.
DOC_PREVIEW_CHARS = 1500
TRUNCATION_MARKER = "… (truncated)"


def preview(text: str, limit: int = DOC_PREVIEW_CHARS) -> str:
    """*text* trimmed to *limit* chars, with a marker when cut."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n" + TRUNCATION_MARKER


def self_reply(user_id: Optional[str], home_dir: Any) -> str:
    """`/self` — the instance, its owner, then the SOUL and SELF documents.

    The same ``load_self_context`` / ``load_self_doc`` calls the agent's
    construction pins, so the text is what the running agent sees. An
    unreadable document says so; it never reads as "none"."""
    from core.instance import (FRAMEWORK_NAME, load_self_context, load_self_doc,
                               owner_label, resolve_instance_id)
    instance_id = resolve_instance_id()
    uid = user_id or "local"
    lines = [f"framework: {FRAMEWORK_NAME}", f"instance: {instance_id}",
             f"owner: {owner_label()}", f"user: {uid}", ""]
    for head, load, missing in (
            ("SOUL (operator-authored, frozen)", lambda: load_self_context(home_dir),
             "SOUL/identity doc authored"),
            ("SELF (agent-writable identity doc)",
             lambda: load_self_doc(home_dir, uid, instance_id), "SELF/identity doc authored")):
        lines.append(f"--- {head} ---")
        try:
            text = load()
        except Exception as exc:  # an unread document is not an empty one
            lines.append(f"(could not read it: {type(exc).__name__})")
        else:
            lines.append(preview(text) if text else f"(no {missing})")
        lines.append("")
    return "\n".join(lines).rstrip()


def memory_scopes_text(provider: Any, user_id: str) -> str:
    """`/memory scopes`: what is quarantined, by scope (never the findings)."""
    from modules.memory.scope import scopes_enabled
    if provider is None or not hasattr(provider, "list_scopes"):
        return "unavailable(this memory backend has no scopes)"
    try:
        rows = provider.list_scopes(user_id)
    except Exception as exc:
        return f"unavailable({type(exc).__name__}: {exc})"
    head = ("Memory scopes: ON" if scopes_enabled() else
            "Memory scopes: OFF — these rows are already visible as shared recall")
    if not rows:
        return head + "\n  no quarantined memory scopes"
    lines = [f"  {r['label']}  {r['rows']} row(s)" for r in rows]
    return "\n".join([head, *lines, "Promote or purge: polyrob owner memory scopes"])


async def memory_reply(user_id: Optional[str], args: List[str]) -> str:
    """`/memory` (the active provider), `/memory search <words>` (recall over
    the shared view — a goal's quarantined findings never surface here) and
    `/memory scopes`. Reads only."""
    uid = user_id or "local"
    try:
        from modules.memory.registry import get_memory_registry
        provider = get_memory_registry().active()
    except Exception as exc:
        return f"Could not resolve memory provider: {exc}"
    name = getattr(provider, "name", None) if provider is not None else None
    external = bool(getattr(provider, "is_external", False)) if provider is not None else False
    sub = str(args[0]).lower() if args else ""
    if sub == "search":
        query = " ".join(str(a) for a in args[1:]).strip()
        if not query:
            return "Usage: /memory search <query>"
        if provider is None or not external or not callable(getattr(provider, "search", None)):
            return "No searchable memory backend active (recall disabled)."
        try:
            hits = await provider.search(query, user_id=uid, limit=10)
        except Exception as exc:  # an unreadable store is not an empty one
            return (f"Memory could not be read ({type(exc).__name__}: {exc}); "
                    "nothing was searched.")
        text = hits.strip() if isinstance(hits, str) else str(hits or "").strip()
        return text or f"No matches for {query!r}."
    if sub == "scopes":
        return memory_scopes_text(provider, uid)
    if not name or not external:
        return "No external memory backend active (recall disabled)."
    return f"Active memory provider: {name}"


__all__ = ["DOC_PREVIEW_CHARS", "TRUNCATION_MARKER", "memory_reply",
           "memory_scopes_text", "preview", "self_reply"]
