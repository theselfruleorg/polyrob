"""061 — `/thread`: the ONE owner transcript, every rail, every seat.

The twin of ``contacts_ops`` for the OWNER: what the agent told the owner (on
any rail — a cron report, an approval notice, a chat reply, a `message` tool
send) and what the owner said back (Telegram, console, REPL), newest-last.
Shared with the REPL (`cli/ui/commands/h_owner_reach.py`), so the two seats
cannot drift. A READ never creates the store; an unreadable one is NAMED.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

USAGE = ("/thread [n | <hours>h] — my conversation with you across every session "
         "and rail (default: last 20 lines). Example: /thread 6h")

_DEFAULT_ROWS = 20
_MAX_ROWS = 60
_MAX_CHARS = 3500


def _parse(args: List[str]) -> tuple:
    """``(limit, hours)`` from ``[n]`` or ``[<h>h]``."""
    tok = (args[0] if args else "").strip().lower()
    if not tok:
        return _DEFAULT_ROWS, None
    if tok.endswith("h") and tok[:-1].isdigit():
        return _MAX_ROWS, max(1, min(int(tok[:-1]), 24 * 14))
    if tok.isdigit():
        return max(1, min(int(tok), _MAX_ROWS)), None
    return _DEFAULT_ROWS, None


def history_reply(user_id: Optional[str], data_dir: str, args: List[str],
                  container: Any = None) -> str:
    """One chat-ready string. Never raises."""
    if not user_id:
        return "Only the owner can read our conversation."
    limit, hours = _parse([str(a) for a in (args or []) if str(a).strip()])
    try:
        from core.surfaces.owner_thread import (owner_thread_enabled, render_transcript,
                                                thread_recent)
        if not owner_thread_enabled():
            return "The owner thread is off (OWNER_THREAD_ENABLED) — nothing is recorded."
        rows = thread_recent(container, str(user_id), limit=limit, hours=hours,
                             data_dir=data_dir)
    except Exception as exc:
        return (f"our conversation: unavailable({type(exc).__name__}: {exc}) — that is "
                f"UNKNOWN, not an empty history.")
    if rows is None:
        return ("no conversation recorded yet — the thread starts with the next line "
                "either of us sends.")
    if not rows:
        window = f"in the last {hours}h" if hours else "yet"
        return f"no lines {window}."
    head = (f"our conversation — last {len(rows)} line(s)"
            + (f" in {hours}h" if hours else "") + ", every session and rail:")
    body = render_transcript(rows)
    if len(body) > _MAX_CHARS:
        body = "…" + body[-_MAX_CHARS:]
    return head + "\n" + body
