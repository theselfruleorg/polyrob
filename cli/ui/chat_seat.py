"""113: the terminal's chats — the same chat rows a chat app has.

The terminal's key is ``agent:main:repl:dm:local:<tenant>``. A terminal run is ONE
session, so it stays in the chat it started in: ``polyrob`` starts in the focused
chat (main unless the person chose another), ``polyrob --new`` opens one first and
``polyrob --chat <number or name>`` picks one. A chat verb that moves the focus ends
the run cleanly and starts the terminal again in that chat (the same command line). The chat row points at the run's
session and the run's episode carries the chat key, so the next start in the same chat
gets the summary of this one (the continuity bridge). Everything here is fail-open: a
chat-store fault must never stop the terminal.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

REPL_SURFACE = "repl"
REPL_CHAT = "local"


def base_key(user_id: str) -> str:
    from core.surfaces.envelopes import SessionSource
    from core.surfaces.session_chat_registry import build_session_key
    return build_session_key(SessionSource(REPL_SURFACE, REPL_CHAT, "dm"), str(user_id))


def registry_for(container: Any):
    """The chat store of this process (opened without the outbound bus), or None."""
    if container is None:
        return None
    try:
        from core.surfaces.bootstrap import ensure_chat_registry
        return ensure_chat_registry(container)
    except Exception:
        logger.debug("terminal chats: no chat store", exc_info=True)
        return None


def attach(container: Any, orchestrator: Any, *, user_id: str, session_id: str,
           new_label: Optional[str] = None, chat_ref: Optional[str] = None) -> dict:
    """Put this run in a chat. Returns ``{"key", "name", "note"}``; ``note`` is a
    line for the person (a ``--chat`` that named no chat), ``key`` None on a fault."""
    out = {"key": None, "name": "", "note": ""}
    try:
        from core.surfaces import chats
        registry = registry_for(container)
        if registry is None:
            return out
        base = base_key(user_id)
        if new_label is not None:
            chats.open_chat(registry, base_key=base, user_id=str(user_id),
                            surface_id=REPL_SURFACE, chat_id=REPL_CHAT, label=new_label)
        elif chat_ref:
            view, error = chats.focus_chat(registry, base_key=base, user_id=str(user_id),
                                           ref=chat_ref)
            if view is None:
                out["note"] = error
        key = chats.focused_key(registry, base, str(user_id))
        registry.bind(key, session_id, str(user_id), REPL_SURFACE, REPL_CHAT)
        orchestrator._chat_thread_key = key   # the run's episode carries the chat
        out.update(key=key, name=chats.chat_name(registry.chat_row(key) or {}))
    except Exception:
        logger.warning("terminal chats: this run has no chat row", exc_info=True)
    return out


#: Set by a chat verb that moved the focus: the terminal starts again in that chat.
_RESTART = {"wanted": False}
RESTARTING = "Starting again in that chat…"


def can_restart() -> bool:
    """A person at a terminal. Piped input is not restarted: lines the reader has
    already taken would be lost, so there the switch applies to the next start."""
    import sys
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def run_verb(ctx: Any, verb: str) -> dict:
    """One chat verb typed in the terminal. A terminal run is one session and stays
    in its chat, so a verb that moves the focus asks for a restart in the new chat
    (``restart`` in the result) when a person is at the terminal; else the reply
    says the switch applies to the next start. Returns ``{"text", "restart"}``."""
    from core.surfaces import chats
    registry = registry_for(getattr(ctx, "container", None))
    base = base_key(ctx.user_id)
    current = getattr(getattr(ctx, "orchestrator", None), "_chat_thread_key", None) or base
    live = can_restart()
    out = chats.chat_verb(registry, verb, list(ctx.args or []), base_key=base,
                          user_id=str(ctx.user_id), surface_id=REPL_SURFACE,
                          chat_id=REPL_CHAT, current_key=current, moves=live)
    moved = bool(out["changed"] and out["focused_key"] and out["focused_key"] != current)
    return {"text": out["text"], "restart": moved and live}


def want_restart() -> None:
    _RESTART["wanted"] = True


def restart_argv(argv: list) -> list:
    """The command line for the restart: the same, without a chat choice (the
    focus is already stored)."""
    out, skip = [], False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg == "--new" or arg.startswith("--chat="):
            continue
        if arg == "--chat":
            skip = True
            continue
        out.append(arg)
    return out


def restart_if_wanted() -> None:  # pragma: no cover — replaces the process
    """Called after the run has closed cleanly (its episode is written)."""
    if not _RESTART["wanted"]:
        return
    import os
    import sys
    _RESTART["wanted"] = False
    sys.stdout.flush()
    sys.stderr.flush()
    argv = restart_argv(list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv]))
    os.execv(sys.executable, argv)
