"""113: the console's chats — the same chat rows every seat has.

The console addresses a chat by its page (``/c/<id>``), so it has no "focused"
chat: it has ONE MAIN chat, which the ``Chat`` destination (``/``) opens, and the
separate chats beside it (``/?new=1`` starts one). The tenant's first console chat
becomes its main chat; every later one is a separate chat row. The rows live in
``session_chat_map`` under ``agent:main:webview:dm:console:<tenant>``, so ``/chats``
on a chat app or in the terminal lists them as chats on another seat.

⚠️ Every function takes the tenant from ``pages._effective_user_id`` and reads only
that tenant's rows. A slug of another tenant is a 404. Reads are fail-open (a chat
store fault must never stop a page): the console then behaves as it did before.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from core.event_kinds import CONSOLE_CHAT_VERB
from webview import webgate
from webview.audit import console_write
from webview.copy import t
from webview.pages_new import api_router

logger = logging.getLogger(__name__)

SURFACE = "webview"
CHAT = "console"


def base_key(user_id: str) -> str:
    from core.surfaces.envelopes import SessionSource
    from core.surfaces.session_chat_registry import build_session_key
    return build_session_key(SessionSource(SURFACE, CHAT, "dm"), str(user_id))


def _registry():
    """The chat store of this process (opened without the outbound bus), or None."""
    try:
        from core.container import DependencyContainer
        from core.surfaces.bootstrap import ensure_chat_registry
        return ensure_chat_registry(DependencyContainer.get_instance())
    except Exception:
        logger.debug("console chats: no chat store", exc_info=True)
        return None


def _tenant(request: Request) -> str:
    from webview.pages import _effective_user_id
    return str(_effective_user_id(request))


def _live_main(registry: Any, user_id: str) -> str:
    """The session id of the tenant's main console chat, or "" (none, or its
    folder is gone)."""
    from webview.chat_open import chat_exists, valid_session_id
    row = registry.chat_row(base_key(user_id)) or {}
    sid = str(row.get("session_id") or "")
    if str(row.get("user_id") or "") != str(user_id) or not valid_session_id(sid):
        return ""
    return sid if chat_exists(sid) else ""


#: How long a chat that `/new [name]` opened in the chat box waits for its page. An
#: older one is not "the chat the person is about to start" any more.
WAITING_SECS = 900


def _fresh(row: Dict[str, Any]) -> bool:
    import time
    try:
        return time.time() - float(row.get("created_at") or 0) <= WAITING_SECS
    except (TypeError, ValueError):
        return False


def _close_waiting(registry: Any, rows: List[Dict[str, Any]]) -> None:
    """A second `/new` replaces the first: a chat with no line is not kept as a row."""
    for row in rows:
        if (row.get("slug") and not row.get("session_id")
                and row.get("closed_at") is None):
            registry.set_closed(row["session_key"], closed=True)


def bind_created(request: Request, response: Any) -> None:
    """A chat the console just created gets its row: the main chat when the tenant
    has none, else a separate chat. Never raises."""
    try:
        session_id = str(getattr(response, "session_id", "") or "")
        registry = _registry()
        if not session_id or registry is None:
            return
        user_id = _tenant(request)
        key = base_key(user_id)
        if _live_main(registry, user_id):
            from core.surfaces import chats
            # `/new [name]` in the chat box opened a row with no line yet: this is it.
            waiting = registry.chat_row(registry.focused_key(key, user_id)) or {}
            if (waiting.get("slug") and not waiting.get("session_id")
                    and _fresh(waiting) and waiting.get("closed_at") is None
                    and str(waiting.get("user_id") or "") == user_id):
                key = waiting["session_key"]
            else:
                key = chats.open_chat(registry, base_key=key, user_id=user_id,
                                      surface_id=SURFACE, chat_id=CHAT)["session_key"]
        registry.bind(key, session_id, user_id, SURFACE, CHAT)
    except Exception:
        logger.warning("console chats: the new chat has no row", exc_info=True)


def main_redirect(request: Request) -> Optional[RedirectResponse]:
    """``/`` opens the main chat when the tenant has one; None = the cold open."""
    try:
        registry = _registry()
        sid = _live_main(registry, _tenant(request)) if registry is not None else ""
        return RedirectResponse(f"/c/{sid}", status_code=303) if sid else None
    except Exception:
        logger.debug("console chats: no main chat redirect", exc_info=True)
        return None


def annotate(request: Request, sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add ``chat: {slug, label, main}`` to each catalog row that is one of the
    caller's console chats. A fault leaves the rows as they are."""
    try:
        registry = _registry()
        if registry is None or not sessions:
            return sessions
        user_id = _tenant(request)
        by_session = {str(r.get("session_id") or ""): r
                      for r in registry.chat_rows(user_id=user_id, base_key=base_key(user_id))
                      if r.get("closed_at") is None}
        for session in sessions:
            row = by_session.get(str(session.get("id") or ""))
            if row is not None:
                slug = row.get("slug") or "main"
                session["chat"] = {"slug": slug, "label": row.get("label") or "",
                                   "main": slug == "main",
                                   "active_iso": _last_active(str(session.get("id") or ""))}
    except Exception:
        logger.debug("console chats: catalog not annotated", exc_info=True)
    return sessions


def _last_active(session_id: str) -> Optional[str]:
    """When the chat last changed (its feed folder, else its folder), UTC ISO; None =
    not known. The chat column orders the separate chats by it (113 W6, IA-1)."""
    try:
        from datetime import datetime, timezone
        from webview.chat_open import _session_folder
        folder = _session_folder(session_id)
        if folder is None:
            return None
        feed = folder / "feed"
        when = max(p.stat().st_mtime for p in (folder, feed) if p.exists())
        return datetime.fromtimestamp(when, timezone.utc).isoformat()
    except Exception:
        logger.debug("console chats: no last activity for %s", session_id, exc_info=True)
        return None


def _page(chat: Dict[str, Any]) -> str:
    sid = str(chat.get("session_id") or "")
    return f"/c/{sid}" if sid else ""


def box_verb(user_id: str, session_id: str, verb: str, args: List[str]) -> str:
    """One chat verb typed in the chat box of the page ``/c/<session_id>``. The
    console has no focus, so a verb that moves answers with the PAGE of that chat;
    ``/rename`` and a bare ``/close`` act on the chat of this page. Never raises."""
    registry = _registry()
    if registry is None:
        return t("chats.unreadable")
    from core.surfaces import chats
    user_id, key = str(user_id), base_key(user_id)
    try:
        rows = registry.chat_rows(user_id=user_id, base_key=key)
    except Exception:
        logger.warning("console chats: the store did not answer", exc_info=True)
        return t("chats.unreadable")
    here = next((r for r in rows if session_id and r.get("closed_at") is None
                 and str(r.get("session_id") or "") == str(session_id)), None)
    if verb in ("/rename", "/close") and here is None and not (verb == "/close" and args):
        return t("chats.box_no_row")
    if verb == "/new":
        _close_waiting(registry, rows)
    out = chats.chat_verb(registry, verb, args, base_key=key, user_id=user_id,
                          surface_id=SURFACE, chat_id=CHAT, page_of=_page,
                          # a page with no row is "in" no chat: a key no row has
                          current_key=here["session_key"] if here else key + ":none")
    text = out["text"]
    if not out["changed"]:
        return text
    if out["closed_key"]:
        agent = webgate.in_process_task_agent()
        if agent is not None and hasattr(agent, "close_chat_episode"):
            agent.close_chat_episode(out["closed_key"])
    console_write(CONSOLE_CHAT_VERB, user_id=user_id, attrs={"verb": verb, "box": True})
    if verb == "/new":
        return text + " " + t("chats.box_new")
    if verb in ("/chat", "/close"):
        target = registry.chat_row(out["focused_key"] or key) or {}
        page = _page(target) if verb == "/chat" else _page(registry.chat_row(key) or {})
        if verb == "/chat":
            return (t("chats.box_open").format(name=chats.chat_name(target), page=page)
                    if page else text + " " + t("chats.box_new"))
        return text + (" " + t("chats.box_main").format(page=page) if page else "")
    return text


def _change(request: Request, verb: str, slug: str, args: List[str]) -> JSONResponse:
    registry = _registry()
    if registry is None:
        raise HTTPException(status_code=503, detail=t("chats.unreadable"))
    from core.surfaces import chats
    user_id = _tenant(request)
    key = base_key(user_id)
    row = next((r for r in registry.chat_rows(user_id=user_id, base_key=key)
                if (r.get("slug") or "main") == slug), None)
    if row is None:
        raise HTTPException(status_code=404)
    out = chats.chat_verb(registry, verb, args, base_key=key, user_id=user_id,
                          surface_id=SURFACE, chat_id=CHAT,
                          current_key=row["session_key"])
    if not out["changed"]:
        return JSONResponse({"success": False, "error": out["text"]}, status_code=400)
    if out["closed_key"]:
        agent = webgate.in_process_task_agent()
        if agent is not None and hasattr(agent, "close_chat_episode"):
            agent.close_chat_episode(out["closed_key"])
    console_write(CONSOLE_CHAT_VERB, user_id=user_id, attrs={"verb": verb, "slug": slug})
    return JSONResponse({"success": True, "message": out["text"], "slug": slug})


@api_router.post("/api/webgate/chats/{slug}/rename", dependencies=webgate.MUTATION_DEPS)
async def api_chat_rename(request: Request, slug: str, body: Dict[str, Any]):
    """Name one of the caller's console chats (``{"label": "..."}``)."""
    return _change(request, "/rename", slug, str((body or {}).get("label") or "").split())


@api_router.post("/api/webgate/chats/{slug}/close", dependencies=webgate.MUTATION_DEPS)
async def api_chat_close(request: Request, slug: str):
    """Close one separate console chat. The main chat does not close."""
    return _change(request, "/close", slug, [])
