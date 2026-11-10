"""113: one chat model for every seat — the logic over ``session_chat_map``.

A CHAT is one row of the registry (``core/surfaces/session_chat_registry.py``): a
seat's ``main`` chat (the seat's base key, continuous) or a separate chat the person
opened beside it (``…:chat:c<n>``). Exactly one open chat per seat is FOCUSED: the
one a new line goes into. This module is pure logic over the registry — no seat
imports — and holds the ONE verb parser and the ONE text renderer; Telegram, the
terminal and the console wrap :func:`chat_verb`.

⚠️ Tenancy. Every read and every change takes the caller's ``user_id`` and filters
on it. A slug, a number or a name resolves inside that tenant's rows only; a row of
another tenant answers "no such chat", never "refused" (its existence is not told).
"Every seat" in a list means every seat of THIS tenant.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, List, Optional

from core.surfaces.session_chat_registry import (
    MAIN_SLUG, chat_session_key, row_from_session_key, split_chat_key)

logger = logging.getLogger(__name__)

#: A name the person gives a chat: short, one line, no control characters.
MAX_LABEL = 40
#: The verbs this module answers (``core/verbs.py`` holds their rows).
CHAT_VERBS = ("/new", "/chats", "/chat", "/rename", "/close")
#: The two local seats; a chat surface's word is its catalog label.
_LOCAL_SEAT_WORDS = {"repl": "the terminal", "webview": "the console"}


def seat_word(surface_id: str) -> str:
    """What a person reads for a seat id (``core/surfaces/catalog.py`` owns the labels)."""
    sid = str(surface_id or "")
    if sid in _LOCAL_SEAT_WORDS:
        return _LOCAL_SEAT_WORDS[sid]
    try:
        from core.surfaces import catalog
        spec = catalog.get(sid)
        if spec is not None:
            return spec.label
    except Exception:
        logger.debug("chats: no catalog label for %s", sid, exc_info=True)
    return sid or "another seat"


UNREADABLE = "I could not read the chats ({reason}). Nothing was changed."


def clean_label(text: Any) -> str:
    """The stored form of a name: one line, trimmed, bounded."""
    label = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or "")).strip()
    return re.sub(r"\s+", " ", label)[:MAX_LABEL].strip()


def _is_person_chat(row: dict) -> bool:
    """A direct chat of a person — not a group, not a group-admin chat, not a send key."""
    parsed = row_from_session_key(row.get("session_key") or "")
    if parsed is None or parsed.get("chat_type") != "dm":
        return False
    return ":admin:" not in str(row.get("session_key") or "")


def _last_activity(row: dict) -> float:
    try:
        return float(row.get("updated_at") or row.get("created_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _view(row: dict) -> dict:
    return {
        "session_key": row.get("session_key"),
        "base_key": row.get("base_key") or split_chat_key(row.get("session_key") or "")[0],
        "slug": row.get("slug") or MAIN_SLUG,
        "label": row.get("label") or "",
        "seat": row.get("surface_id") or "",
        "session_id": row.get("session_id") or "",
        "focused": bool(row.get("focused")) and row.get("closed_at") is None,
        "closed": row.get("closed_at") is not None,
        "last_activity": _last_activity(row),
        "owner_pid": row.get("owner_pid"),
    }


def list_chats(registry: Any, *, user_id: str, base_key: Optional[str] = None,
               include_closed: bool = False) -> dict:
    """``{"chats": [...], "error": None}`` for ONE tenant: main first, then by last
    activity. ``base_key`` narrows it to one seat. An unreadable store is
    ``{"chats": None, "error": <reason>}`` — never an empty list."""
    if registry is None:
        return {"chats": None, "error": "no chat store in this process"}
    try:
        rows = registry.chat_rows(user_id=str(user_id), base_key=base_key)
    except Exception as exc:
        logger.warning("chats: the store did not answer", exc_info=True)
        return {"chats": None, "error": type(exc).__name__}
    views = [_view(r) for r in rows if _is_person_chat(r)]
    if not include_closed:
        views = [v for v in views if not v["closed"]]
    views.sort(key=lambda v: (v["slug"] != MAIN_SLUG, -v["last_activity"]))
    return {"chats": views, "error": None}


def focused_key(registry: Any, base_key: str, user_id: Optional[str] = None) -> str:
    """The key a new line on this seat goes to: the focused open chat, else main.
    With ``user_id``, a row of another tenant is never the answer. Fail-open to main:
    a store fault must never drop a person's line."""
    try:
        return registry.focused_key(base_key, user_id)
    except Exception:
        logger.warning("chats: focus read failed (main is used)", exc_info=True)
    return str(base_key)


def _seat_rows(registry: Any, base_key: str, user_id: str) -> List[dict]:
    """This tenant's open-or-closed chats on this seat, main first. Main is ALWAYS in
    the list: before its first line it has no row, and the list a person sees
    (``chats_reply``) shows it as number 1 — a number or ``main`` must mean the same
    chat here. That stand-in carries ``no_row``."""
    rows = [r for r in registry.chat_rows_for_base(base_key)
            if str(r.get("user_id") or "") == str(user_id)]
    rows.sort(key=lambda r: ((r.get("slug") or MAIN_SLUG) != MAIN_SLUG, -_last_activity(r)))
    if not any((r.get("slug") or MAIN_SLUG) == MAIN_SLUG for r in rows):
        rows.insert(0, {"session_key": str(base_key), "base_key": str(base_key),
                        "slug": MAIN_SLUG, "label": "", "user_id": str(user_id),
                        "closed_at": None, "focused": 0, "session_id": "", "no_row": True})
    return rows


def _resolve_ref(rows: List[dict], ref: str) -> tuple:
    """``(row, error)`` for a number (the list order of the OPEN chats), a slug, or a
    unique name prefix. ``main`` always resolves, even before its row exists."""
    ref = str(ref or "").strip()
    open_rows = [r for r in rows if r.get("closed_at") is None]
    if not ref:
        return (None, "Name a chat: its number in /chats, or its name.")
    if ref.isdigit():
        index = int(ref)
        if 1 <= index <= len(open_rows):
            return (open_rows[index - 1], "")
        return (None, f"No chat number {index}. /chats shows the list.")
    low = ref.lower()
    for row in rows:
        if (row.get("slug") or MAIN_SLUG) == low:
            return (row, "")
    named = [r for r in rows if (r.get("label") or "").lower().startswith(low)]
    exact = [r for r in named if (r.get("label") or "").lower() == low]
    if len(exact) == 1 or len(named) == 1:
        return ((exact or named)[0], "")
    if len(named) > 1:
        return (None, f"More than one chat starts with {ref!r}. Use its number in /chats.")
    return (None, f"No chat named {ref!r}. /chats shows the list.")


def _next_slug(rows: List[dict]) -> str:
    used = [int(m.group(1)) for m in
            (re.fullmatch(r"c(\d+)", r.get("slug") or "") for r in rows) if m]
    return f"c{max(used + [1]) + 1}"


def _name(row_or_view: dict) -> str:
    slug = row_or_view.get("slug") or MAIN_SLUG
    label = row_or_view.get("label") or ""
    if slug == MAIN_SLUG:
        return f"main ({label})" if label else "main"
    return label or slug


def chat_name(row_or_view: dict) -> str:
    """What a person reads for a chat: its name, else ``main`` or its slug."""
    return _name(row_or_view)


def open_chat(registry: Any, *, base_key: str, user_id: str, surface_id: str,
              chat_id: str, label: str = "") -> dict:
    """Open a separate chat on this seat and focus it. No session is created: the
    first line creates it, as on any cold start. Returns the new chat's view."""
    rows = _seat_rows(registry, base_key, user_id)
    for _ in range(5):   # another process can take the slug between read and insert
        key = chat_session_key(base_key, _next_slug(rows))
        if registry.insert_chat(key, user_id=str(user_id), surface_id=surface_id,
                                chat_id=str(chat_id), label=clean_label(label)):
            registry.set_focus(base_key, key)
            return _view(registry.chat_row(key) or {"session_key": key})
        rows = _seat_rows(registry, base_key, user_id)
    raise RuntimeError("could not open a chat (the store kept refusing the new row)")


def focus_chat(registry: Any, *, base_key: str, user_id: str, ref: str) -> tuple:
    """``(view, error)``. Focusing ``main`` before its row exists just clears the focus."""
    rows = _seat_rows(registry, base_key, user_id)
    row, error = _resolve_ref(rows, ref)
    if row is None:
        return (None, error)
    if row.get("no_row"):   # main before its first line: clear the focus
        registry.set_focus(base_key, base_key)
        return ({"session_key": base_key, "slug": MAIN_SLUG, "label": ""}, "")
    if row.get("closed_at") is not None:
        registry.set_closed(row["session_key"], closed=False)
    registry.set_focus(base_key, row["session_key"])
    return (_view(registry.chat_row(row["session_key"]) or row), "")


def rename_chat(registry: Any, *, base_key: str, user_id: str, label: str,
                ref: Optional[str] = None) -> tuple:
    """Name the focused chat (or ``ref``). ``(view, error)``."""
    label = clean_label(label)
    if not label:
        return (None, "Give the chat a name: /rename <name>.")
    rows = _seat_rows(registry, base_key, user_id)
    if ref:
        row, error = _resolve_ref(rows, ref)
    else:
        key = focused_key(registry, base_key, user_id)
        row = next((r for r in rows if r.get("session_key") == key), None)
        error = ""
    if row is not None and row.get("no_row"):
        row = None
    if row is None:
        return (None, error or "This chat has no messages yet. Say something first, then name it.")
    registry.set_label(row["session_key"], label)
    return (_view(registry.chat_row(row["session_key"]) or row), "")


def close_chat(registry: Any, *, base_key: str, user_id: str,
               ref: Optional[str] = None) -> tuple:
    """Close a chat (the focused one by default); the focus returns to main. Main is
    never closed. ``(view, error)`` — the caller writes the chat's episode."""
    rows = _seat_rows(registry, base_key, user_id)
    if ref:
        row, error = _resolve_ref(rows, ref)
    else:
        key = focused_key(registry, base_key, user_id)
        row = next((r for r in rows if r.get("session_key") == key), None)
        error = "" if row is not None else "The main chat stays open. Nothing was closed."
    if row is None:
        return (None, error)
    if (row.get("slug") or MAIN_SLUG) == MAIN_SLUG:
        return (None, "The main chat stays open. /new opens a separate chat.")
    if row.get("closed_at") is not None:
        return (None, f"{_name(row)} is closed already.")
    was_focused = bool(row.get("focused"))
    registry.set_closed(row["session_key"], closed=True)
    if was_focused:
        registry.set_focus(base_key, base_key)
    return (_view(registry.chat_row(row["session_key"]) or row), "")


def _age(seconds: float) -> str:
    if seconds < 90:
        return "now"
    if seconds < 5400:
        return f"{int(round(seconds / 60))} min ago"
    if seconds < 48 * 3600:
        return f"{int(round(seconds / 3600))} h ago"
    return f"{int(round(seconds / 86400))} d ago"


def chat_tap(slug: str) -> str:
    """The one-token form of ``/chat <slug>`` (a chat app links the whole token)."""
    return f"/chat_{slug or MAIN_SLUG}"


_TAP_RE = re.compile(r"^/chat_(main|c[1-9][0-9]{0,5})(?:@\w+)?$")


def parse_chat_tap(token: str) -> str:
    """The slug of a ``/chat_<slug>`` token, or ``""``. The slug is looked up in
    the sender's own rows; the token carries no authority."""
    match = _TAP_RE.match(str(token or "").strip().lower())
    return match.group(1) if match else ""


def reply_prefix(registry: Any, session_key: str) -> str:
    """``"[name] "`` for a line that comes from a chat the person is NOT in now (a
    turn that ended after they switched, a background report), else ``""``. Never
    raises: a label must not cost a reply."""
    try:
        row = registry.chat_row(session_key) if registry is not None else None
        if row is None or not _is_person_chat(row):
            return ""
        base_key = row.get("base_key") or split_chat_key(session_key)[0]
        if focused_key(registry, base_key, row.get("user_id")) == session_key:
            return ""
        return f"[{_name(row)}] "
    except Exception:
        logger.debug("chats: no reply prefix (fail-open)", exc_info=True)
        return ""


def chats_reply(registry: Any, *, base_key: str, user_id: str,
                now: Optional[float] = None, taps: bool = False,
                current_key: Optional[str] = None, page_of: Any = None) -> str:
    """The list every seat shows: this seat's chats, numbered (the number is what
    ``/chat <n>`` takes), then the tenant's chats on other seats, named by seat.
    ``page_of(chat) -> str`` is for a seat that addresses a chat by its page (the
    console): each row then ends with its page and only the open page is marked."""
    listing = list_chats(registry, user_id=user_id)
    if listing["error"]:
        return UNREADABLE.format(reason=listing["error"])
    now = time.time() if now is None else now
    mine = [c for c in listing["chats"] if c["base_key"] == base_key]
    if not any(c["slug"] == MAIN_SLUG for c in mine):   # main before its first line
        mine.insert(0, {"slug": MAIN_SLUG, "label": "", "focused": not any(
            c["focused"] for c in mine), "last_activity": 0.0, "session_id": ""})
    lines = ["Your chats here:"]
    for index, chat in enumerate(mine, start=1):
        mark = "  ← you are here" if chat["focused"] else ""
        if current_key is not None:   # a seat whose run stays in the chat it started in
            here = chat.get("session_key") == current_key or (
                chat["slug"] == MAIN_SLUG and current_key == base_key)
            mark = ("  ← you are here" if here and (chat["focused"] or page_of) else
                    "" if page_of else
                    "  ← this run" if here else
                    "  ← your next start" if chat["focused"] else "")
        when = _age(now - chat["last_activity"]) if chat["last_activity"] else "no messages yet"
        tap = f"  {chat_tap(chat['slug'])}" if taps and not chat["focused"] else ""
        page = str(page_of(chat) or "") if page_of else ""
        lines.append(f"{index}. {_name(chat)} · {when}{mark}{tap}" + (f"  {page}" if page else ""))
    others = [c for c in listing["chats"] if c["base_key"] != base_key]
    if others:
        lines.append("")
        lines.append("On your other seats:")
        for chat in others:
            seat = seat_word(chat["seat"])
            lines.append(f"- {_name(chat)} · {seat} · {_age(now - chat['last_activity'])}")
    lines.append("")
    lines.append(("/chat <number or name> gives its page" if page_of else
                  "/chat <number or name> switches") + " · /new [name] opens one · "
                 "/rename <name> · /close")
    return "\n".join(lines)


#: The tail for a seat whose run cannot move to another chat (the terminal).
NEXT_START = ("This run stays in the chat it started in. Leave and start me again "
              "to continue in {name}.")


def chat_verb(registry: Any, verb: str, args: List[str], *, base_key: str, user_id: str,
              surface_id: str, chat_id: str, taps: bool = False,
              current_key: Optional[str] = None, moves: bool = True,
              page_of: Any = None) -> dict:
    """Run one chat verb for ONE tenant on ONE seat. Returns ``{"text", "changed",
    "closed_key", "focused_key"}``: ``closed_key`` is the key whose episode the caller
    writes; ``focused_key`` is where the seat's next line goes. Never raises."""
    out = {"text": "", "changed": False, "closed_key": None, "focused_key": None}
    verb = "/" + str(verb or "").strip().lstrip("/").lower()
    rest = " ".join(args or []).strip()
    if registry is None:
        out["text"] = UNREADABLE.format(reason="no chat store in this process")
        return out
    # "the chat you are in": the focused chat, or this run's chat on a seat that says so
    here = split_chat_key(current_key)[1] if current_key else None
    try:
        if verb == "/chats":
            out["text"] = chats_reply(registry, base_key=base_key, user_id=user_id, taps=taps,
                                      current_key=current_key, page_of=page_of)
        elif verb == "/new":
            view = open_chat(registry, base_key=base_key, user_id=user_id,
                             surface_id=surface_id, chat_id=chat_id, label=rest)
            out.update(changed=True, focused_key=view["session_key"])
            out["text"] = (f"New chat: {_name(view)}. Your next message starts it. "
                           "/chat main goes back; /chats shows every chat."
                           if current_key is None else f"New chat: {_name(view)}."
                           if moves else
                           f"New chat: {_name(view)}. " + NEXT_START.format(name=_name(view)))
        elif verb == "/chat":
            if not rest:
                out["text"] = chats_reply(registry, base_key=base_key, user_id=user_id, taps=taps,
                                      current_key=current_key, page_of=page_of)
                return out
            view, error = focus_chat(registry, base_key=base_key, user_id=user_id, ref=rest)
            if view is None:
                out["text"] = error
            else:
                out.update(changed=True, focused_key=view["session_key"])
                out["text"] = (f"Now in chat: {_name(view)}." if moves or current_key in (
                    None, view["session_key"]) else
                    f"{_name(view)} is your chat here now. " + NEXT_START.format(name=_name(view)))
        elif verb == "/rename":
            view, error = rename_chat(registry, base_key=base_key, user_id=user_id,
                                      label=rest, ref=here)
            out["text"] = error if view is None else f"This chat is now named: {_name(view)}."
            out["changed"] = view is not None
        elif verb == "/close":
            view, error = close_chat(registry, base_key=base_key, user_id=user_id,
                                     ref=rest or here)
            if view is None:
                out["text"] = error
            else:
                out.update(changed=True, closed_key=view["session_key"],
                           focused_key=focused_key(registry, base_key, user_id))
                out["text"] = (f"Closed: {_name(view)}. You are in the main chat."
                               if current_key is None else f"Closed: {_name(view)}.")
        else:
            out["text"] = "Chat verbs: /new [name], /chats, /chat <number or name>, /rename <name>, /close."
    except Exception as exc:
        logger.warning("chat verb %s failed", verb, exc_info=True)
        out.update(changed=False, closed_key=None, focused_key=None)
        out["text"] = UNREADABLE.format(reason=type(exc).__name__)
    return out
