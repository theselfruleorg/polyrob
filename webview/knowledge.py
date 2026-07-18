"""/knowledge — the owner-facing knowledge wiki (C2, 2026-07-11), READ-ONLY v1.

One page + JSON read endpoints following the webgate-v1 pages contract
(``webview/pages.py``): every endpoint REUSES an existing reader — the notes
verbs on the active MemoryProvider (C1), ``recall_episodes``, ``kb_list_sources``,
``SkillManager`` catalog/pending, and the durable telemetry event log — it never
grows a second source of truth. Fail-open throughout: a missing provider /
disabled flag / read error degrades to an empty result, never a 500.

Tenancy: ``_effective_user_id`` (imported from ``webview.pages``) is called
OUTSIDE every fail-open try — its multitenant 403 must never be swallowed.

Write actions are deliberately absent (pending-skill review stays in the CLI:
``/pending`` · ``polyrob owner pending``). NO ``from __future__ import
annotations`` (module convention).
"""
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from webview.pages import (
    _TEMPLATES,
    _data_dir,
    _effective_user_id,
    _memory_provider,
    _page_context,
)

router = APIRouter()

_CHANGE_KINDS = ("self_modification", "memory_write")


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
        return JSONResponse({"items": [], "count": 0, "status": status})
    try:
        notes = await provider.note_list(user_id, status=status,
                                         tag=tag or None, limit=limit)
    except Exception:
        notes = []
    for n in notes:
        n["updated_day"] = _fmt_day(n.get("updated_ts"))
        n["created_day"] = _fmt_day(n.get("created_ts"))
    return JSONResponse({"items": notes, "count": len(notes), "status": status})


@router.get("/api/webgate/knowledge/note/{note_id}")
async def api_knowledge_note(request: Request, note_id: int):
    """One note + its backlinks ("learned from" provenance included)."""
    provider = _memory_provider()
    user_id = _effective_user_id(request)
    if provider is None or not hasattr(provider, "note_get"):
        return JSONResponse({"note": None, "backlinks": []}, status_code=404)
    try:
        # bump_access=False: this page is READ-ONLY — an owner browsing the wiki
        # must not mint the agent-reuse signal the staleness curator keys on.
        note = await provider.note_get(user_id, note_id, bump_access=False)
    except Exception:
        note = None
    if note is None:
        return JSONResponse({"note": None, "backlinks": []}, status_code=404)
    note["updated_day"] = _fmt_day(note.get("updated_ts"))
    note["created_day"] = _fmt_day(note.get("created_ts"))
    backlinks = []
    try:
        if note.get("title"):
            backlinks = await provider.note_backlinks(user_id, note["title"])
    except Exception:
        backlinks = []
    return JSONResponse({"note": note, "backlinks": backlinks})


# --- episodes ----------------------------------------------------------------#

@router.get("/api/webgate/knowledge/episodes")
async def api_knowledge_episodes(request: Request, since_hours: int = 0,
                                 kind: str = "", limit: int = 20):
    """The episode ledger (runs browser): full rows incl. outcome/artifacts/spend."""
    provider = _memory_provider()
    user_id = _effective_user_id(request)
    if provider is None or not hasattr(provider, "recall_episodes"):
        return JSONResponse({"items": [], "count": 0})
    since_ts = None
    try:
        if int(since_hours) > 0:
            since_ts = int(time.time()) - int(since_hours) * 3600
    except (TypeError, ValueError):
        since_ts = None
    try:
        eps = await provider.recall_episodes(
            user_id=user_id, since_ts=since_ts, kind=(kind or None), limit=limit)
    except Exception:
        eps = []
    items = []
    for e in eps:
        items.append({
            "ts": e.ts, "day": _fmt_day(e.ts), "session_id": e.session_id,
            "kind": e.kind, "task": e.task, "outcome": e.outcome,
            "summary": e.summary, "artifacts": e.artifacts or [],
            "spend_usd": e.spend_usd, "steps": e.steps, "goal_id": e.goal_id,
        })
    return JSONResponse({"items": items, "count": len(items)})


# --- skills ------------------------------------------------------------------#

@router.get("/api/webgate/knowledge/skills")
async def api_knowledge_skills(request: Request):
    """Skill catalog + reuse stats + pending drafts (previously web-invisible)."""
    user_id = _effective_user_id(request)
    catalog, pending, usage = [], [], {}
    try:
        from modules.skills.skill_usage import get_skill_usage_store
        rows = get_skill_usage_store(_data_dir()).list_authored(user_id=user_id)
        usage = {r["skill_id"]: r for r in rows}
    except Exception:
        usage = {}
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
    except Exception:
        pass
    return JSONResponse({"catalog": catalog, "pending": pending,
                         "count": len(catalog)})


@router.get("/api/webgate/knowledge/skill/{skill_id}")
async def api_knowledge_skill(request: Request, skill_id: str):
    """One skill's SKILL.md body + provenance/reuse. Read-only."""
    user_id = _effective_user_id(request)
    body, provenance, load_count = "", "", 0
    try:
        from agents.task.agent.skill_manager import get_skill_manager
        sm = get_skill_manager()
        body = sm._load_skill_content(skill_id, user_id=user_id) or ""
        if hasattr(sm, "provenance_of"):
            provenance = sm.provenance_of(skill_id, user_id) or ""
    except Exception:
        body = body or ""
    try:
        from modules.skills.skill_usage import get_skill_usage_store
        u = get_skill_usage_store(_data_dir()).get_usage(skill_id, user_id) or {}
        load_count = u.get("load_count", 0)
    except Exception:
        load_count = 0
    if not body:
        return JSONResponse({"skill_id": skill_id, "body": "", "provenance": "",
                             "load_count": 0}, status_code=404)
    return JSONResponse({"skill_id": skill_id, "body": body,
                         "provenance": provenance, "load_count": load_count})


# --- KB ------------------------------------------------------------------- --#

@router.get("/api/webgate/knowledge/kb")
async def api_knowledge_kb(request: Request, collection: str = ""):
    """KB sources for the tenant — reuse ``kb_list_sources``."""
    provider = _memory_provider()
    user_id = _effective_user_id(request)
    if provider is None or not hasattr(provider, "kb_list_sources"):
        return JSONResponse({"items": [], "count": 0})
    try:
        items = await provider.kb_list_sources(user_id=user_id,
                                               collection=(collection or None))
    except Exception:
        items = []
    return JSONResponse({"items": items, "count": len(items)})


# --- recent changes (the wiki changelog) --------------------------------------#

@router.get("/api/webgate/knowledge/changes")
async def api_knowledge_changes(request: Request, limit: int = 50):
    """self_modification + memory_write events from the durable event log — the
    knowledge layer's changelog, zero new plumbing."""
    user_id = _effective_user_id(request)
    limit = max(1, min(int(limit or 50), 200))
    items = []
    try:
        from agents.task.telemetry.event_log import get_event_log
        log = get_event_log()
        for kind in _CHANGE_KINDS:
            for e in (log.query(kind=kind, user_id=user_id, limit=limit) or []):
                e = dict(e)
                e["day"] = _fmt_day(e.get("ts"))
                items.append(e)
        items.sort(key=lambda e: e.get("ts") or 0, reverse=True)
        items = items[:limit]
    except Exception:
        items = []
    return JSONResponse({"items": items, "count": len(items)})


# --- page ----------------------------------------------------------------- --#

@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request):
    return _TEMPLATES.TemplateResponse("knowledge.html", _page_context(request))


__all__ = ["router"]
