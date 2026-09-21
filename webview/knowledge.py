"""/knowledge — the owner-facing knowledge wiki (C2, 2026-07-11), READ-ONLY v1.

One page + JSON read endpoints following the webgate-v1 pages contract
(``webview/pages.py``): every endpoint REUSES an existing reader — the notes
verbs on the active MemoryProvider (C1), ``recall_episodes``, ``kb_list_sources``,
``SkillManager`` catalog/pending, and the durable telemetry event log — it never
grows a second source of truth.

⚠️ **One shape, and an unreadable store is never an empty one** (043 A9). Every
reader answers ``{"items": [...], "count": int, "error": str|null}``; on a read
FAULT ``items`` is ``null`` and ``error`` names the reason. An empty list means
"I read it and it holds nothing" — a different sentence, and the six readers
here used to say it over a store that had refused.

⚠️ There is a THIRD answer, and it is not the second one: a missing provider or
a disabled flag means nothing is CONFIGURED. That is a genuine empty list, so it
keeps ``items: []`` and ``error: None`` and puts one sentence in ``reason`` —
the field the console already renders on its empty state. It rode in ``error``
until the 2026-09-21 revalidation, as the token ``memory_provider_unavailable``,
and every client treats a set ``error`` as a failed read: an instance with no
memory backend was told the console could not read its knowledge base, in a
machine name no owner should ever see.

Tenancy: ``_effective_user_id`` (imported from ``webview.pages``) is called
OUTSIDE every fail-open try — its multitenant 403 must never be swallowed.

Write actions are deliberately absent (pending-skill review stays in the CLI:
``/pending`` · ``polyrob owner pending``). NO ``from __future__ import
annotations`` (module convention).
"""
import time

from fastapi import APIRouter, Request

from fastapi.responses import JSONResponse

from webview.pages import (
    _data_dir,
    _effective_user_id,
    _memory_provider,
)

router = APIRouter()

_CHANGE_KINDS = ("self_modification", "memory_write")


def _not_configured(**extra) -> JSONResponse:
    """The "nothing is configured" answer: a real empty list, ``error: None``,
    and ONE sentence from the copy layer in ``reason``.

    Never a machine token, and never in ``error`` — see the module docstring.
    """
    from webview.copy import t
    body = {"items": [], "count": 0, "error": None,
            "reason": t("agent.memory.not_configured")}
    body.update(extra)
    return JSONResponse(body)


def _why(exc: BaseException) -> str:
    """The short, bounded reason a store refused — one line, never a traceback.

    Same shape ``core.surfaces.inbox.why`` uses, so a reason rendered on the
    Inbox and one rendered on a knowledge panel read alike.
    """
    text = f"{type(exc).__name__}: {exc}".strip()
    return " ".join(text.split())[:200]


def _fmt_day(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts))) if ts else ""
    except Exception:
        return ""


# --- notes ------------------------------------------------------------------ #

@router.get("/api/webgate/knowledge/notes")
async def api_knowledge_notes(request: Request, status: str = "active",
                              tag: str = "", limit: int = 200):
    """The tenant's notes (C1 substrate) by status, newest-updated first."""
    status = status if status in ("active", "pending", "archived") else "active"
    provider = _memory_provider()
    user_id = _effective_user_id(request)  # 403 must not be fail-open-swallowed
    if provider is None or not hasattr(provider, "note_list"):
        return _not_configured(status=status)
    try:
        notes = await provider.note_list(user_id, status=status,
                                         tag=tag or None, limit=limit)
    except Exception as exc:
        return JSONResponse({"items": None, "count": None, "status": status,
                             "error": _why(exc)})
    for n in notes:
        n["updated_day"] = _fmt_day(n.get("updated_ts"))
        n["created_day"] = _fmt_day(n.get("created_ts"))
    return JSONResponse({"items": notes, "count": len(notes), "status": status,
                         "error": None})


@router.get("/api/webgate/knowledge/note/{note_id}")
async def api_knowledge_note(request: Request, note_id: int):
    """One note + its backlinks ("learned from" provenance included)."""
    provider = _memory_provider()
    user_id = _effective_user_id(request)
    if provider is None or not hasattr(provider, "note_get"):
        from webview.copy import t
        return JSONResponse({"note": None, "backlinks": [], "count": 0,
                             "error": None,
                             "reason": t("agent.memory.not_configured")},
                            status_code=404)
    try:
        # bump_access=False: this page is READ-ONLY — an owner browsing the wiki
        # must not mint the agent-reuse signal the staleness curator keys on.
        note = await provider.note_get(user_id, note_id, bump_access=False)
    except Exception as exc:
        # A read that FAILED is not a note that is missing: 200 with the reason,
        # so the page can say which it was.
        return JSONResponse({"note": None, "backlinks": None, "count": None,
                             "error": _why(exc)})
    if note is None:
        return JSONResponse({"note": None, "backlinks": [], "count": 0,
                             "error": None}, status_code=404)
    note["updated_day"] = _fmt_day(note.get("updated_ts"))
    note["created_day"] = _fmt_day(note.get("created_ts"))
    backlinks, error = [], None
    try:
        if note.get("title"):
            backlinks = await provider.note_backlinks(user_id, note["title"])
    except Exception as exc:
        backlinks, error = None, _why(exc)
    return JSONResponse({"note": note, "backlinks": backlinks,
                         "count": len(backlinks or []), "error": error})


# --- episodes ----------------------------------------------------------------#

@router.get("/api/webgate/knowledge/episodes")
async def api_knowledge_episodes(request: Request, since_hours: int = 0,
                                 kind: str = "", limit: int = 20):
    """The episode ledger (runs browser): full rows incl. outcome/artifacts/spend."""
    provider = _memory_provider()
    user_id = _effective_user_id(request)
    if provider is None or not hasattr(provider, "recall_episodes"):
        return _not_configured()
    since_ts = None
    try:
        if int(since_hours) > 0:
            since_ts = int(time.time()) - int(since_hours) * 3600
    except (TypeError, ValueError):
        since_ts = None
    try:
        eps = await provider.recall_episodes(
            user_id=user_id, since_ts=since_ts, kind=(kind or None), limit=limit)
    except Exception as exc:
        return JSONResponse({"items": None, "count": None, "error": _why(exc)})
    items = []
    for e in eps:
        items.append({
            "ts": e.ts, "day": _fmt_day(e.ts), "session_id": e.session_id,
            "kind": e.kind, "task": e.task, "outcome": e.outcome,
            "summary": e.summary, "artifacts": e.artifacts or [],
            "spend_usd": e.spend_usd, "steps": e.steps, "goal_id": e.goal_id,
        })
    return JSONResponse({"items": items, "count": len(items), "error": None})


# --- skills ------------------------------------------------------------------#

@router.get("/api/webgate/knowledge/skills")
async def api_knowledge_skills(request: Request):
    """Skill catalog + reuse stats + pending drafts (previously web-invisible).

    030 D4: a failed CATALOG read is reported via an ``error`` field (same JSON
    shape, still HTTP 200 so the page renders) — a bare ``count: 0`` is
    indistinguishable from a genuinely empty catalog. A9: the failure now also
    makes ``items`` null, and the usage-stats leg names itself in
    ``usage_error`` instead of decorating every row with a silent zero.
    """
    user_id = _effective_user_id(request)
    catalog, pending, usage = [], [], {}
    error = usage_error = None
    try:
        from modules.skills.skill_usage import get_skill_usage_store
        rows = get_skill_usage_store(_data_dir()).list_authored(user_id=user_id)
        usage = {r["skill_id"]: r for r in rows}
    except Exception as exc:
        usage, usage_error = {}, _why(exc)
    try:
        from agents.task.agent.skill_manager import get_skill_manager
        sm = get_skill_manager()
        for m in sm.get_catalog_skills(user_id=user_id, max_skills=200):
            u = usage.get(m.skill_id, {})
            catalog.append({
                "skill_id": m.skill_id,
                "description": m.description,
                "source": m.source,
                "created_by": u.get("created_by", ""),
                "load_count": u.get("load_count", 0),
            })
        if hasattr(sm, "list_pending_skills"):
            pending = sm.list_pending_skills(user_id) or []
    except Exception as exc:
        error = _why(exc)
    payload = {
        "items": (None if error else catalog),
        # ``catalog`` is the legacy key this endpoint has always answered with;
        # ``items`` is the ONE shape every knowledge reader now uses.
        "catalog": (None if error else catalog),
        "pending": pending,
        "count": (None if error else len(catalog)),
        "error": error,
        "usage_error": usage_error,
    }
    return JSONResponse(payload)


@router.get("/api/webgate/knowledge/skill/{skill_id}")
async def api_knowledge_skill(request: Request, skill_id: str):
    """One skill's SKILL.md body + provenance/reuse. Read-only."""
    user_id = _effective_user_id(request)
    body, provenance, load_count = "", "", 0
    error = usage_error = None
    try:
        from agents.task.agent.skill_manager import get_skill_manager
        sm = get_skill_manager()
        body = sm._load_skill_content(skill_id, user_id=user_id) or ""
        if hasattr(sm, "provenance_of"):
            provenance = sm.provenance_of(skill_id, user_id) or ""
    except Exception as exc:
        # A skill whose file could not be READ is not a skill that does not
        # exist — the 404 below would say exactly that.
        return JSONResponse({"skill_id": skill_id, "body": None,
                             "provenance": None, "load_count": None,
                             "error": _why(exc)})
    try:
        from modules.skills.skill_usage import get_skill_usage_store
        u = get_skill_usage_store(_data_dir()).get_usage(skill_id, user_id) or {}
        load_count = u.get("load_count", 0)
    except Exception as exc:
        load_count, usage_error = None, _why(exc)
    if not body:
        return JSONResponse({"skill_id": skill_id, "body": "", "provenance": "",
                             "load_count": 0, "error": None}, status_code=404)
    return JSONResponse({"skill_id": skill_id, "body": body,
                         "provenance": provenance, "load_count": load_count,
                         "error": error, "usage_error": usage_error})


# --- KB ------------------------------------------------------------------- --#

@router.get("/api/webgate/knowledge/kb")
async def api_knowledge_kb(request: Request, collection: str = ""):
    """KB sources for the tenant — reuse ``kb_list_sources``."""
    provider = _memory_provider()
    user_id = _effective_user_id(request)
    if provider is None or not hasattr(provider, "kb_list_sources"):
        return _not_configured()
    try:
        items = await provider.kb_list_sources(user_id=user_id,
                                               collection=(collection or None))
    except Exception as exc:
        return JSONResponse({"items": None, "count": None, "error": _why(exc)})
    return JSONResponse({"items": items, "count": len(items), "error": None})


# --- recent changes (the wiki changelog) --------------------------------------#

@router.get("/api/webgate/knowledge/changes")
async def api_knowledge_changes(request: Request, limit: int = 50):
    """self_modification + memory_write events from the durable event log — the
    knowledge layer's changelog, zero new plumbing."""
    user_id = _effective_user_id(request)
    limit = max(1, min(int(limit or 50), 200))
    items = []
    try:
        from core.event_log import get_event_log
        log = get_event_log()
        for kind in _CHANGE_KINDS:
            for e in (log.query(kind=kind, user_id=user_id, limit=limit) or []):
                e = dict(e)
                e["day"] = _fmt_day(e.get("ts"))
                items.append(e)
        items.sort(key=lambda e: e.get("ts") or 0, reverse=True)
        items = items[:limit]
    except Exception as exc:
        return JSONResponse({"items": None, "count": None, "error": _why(exc)})
    return JSONResponse({"items": items, "count": len(items), "error": None})


__all__ = ["router"]
