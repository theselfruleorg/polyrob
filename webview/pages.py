"""Webgate v1 read-only pages — Memory · Autonomy · Identity · System.

These are the four single-user pages POLYROB lacked (doc 03 §4 P2). Each page is
one Jinja template + one JSON read endpoint here. Every endpoint **reuses the
existing service** — it never reimplements a second source of truth:

- Memory   → the active ``MemoryProvider.search()`` (``modules/memory/*``).
- Autonomy → ``GoalBoard.list()`` (``agents/task/goals/board.py``) +
             ``CronService.list_jobs()`` (``cron/service.py``).
- Identity → ``core/instance.py`` SOUL/SELF (``load_self_context`` /
             ``load_self_doc``) — **read-only**, no web write path (editing stays
             the owner-gated ``self_context_manage`` action).
- System   → ``cli/commands/doctor.py::doctor_report`` (imported, not shelled out).

Web endpoints have no agent ``execution_context``, so they call the services
directly rather than the agent tool-action path — still the same SSOT, not a
rebuild. Everything is fail-open: a missing provider / disabled flag / read error
degrades to an empty result, never a 500.

Flags are read via the dedicated helpers / ``os.environ`` (never ``BotConfig.get``,
which is a ``getattr`` trap — AGENTS.md landmine). NO ``from __future__ import
annotations`` here (kept consistent with the registry-closure landmine elsewhere
and unnecessary in this module).
"""
import os

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from webview import webgate

# Reused services / config — imported at module level so tests can monkeypatch
# them at the ``webview.pages`` seam (the proof of reuse).
from agents.task.constants import AutonomyConfig
from agents.task.goals.board import GoalBoard
from cli.commands.doctor import doctor_report, local_flag_on, resolve_memory_backend
from core.instance import (
    load_pfp_meta,
    load_self_context,
    load_self_doc,
    pfp_path,
    resolve_instance_id,
)
from cron.jobs import CronJobStore
from cron.service import CronService
from core.version import get_version
from modules.credits.unified_ledger import build_ledger

router = APIRouter()


# --- template resolution (same asset base as server.py) --------------------- #

def _templates() -> Jinja2Templates:
    try:
        from core.assets import webgate_asset_dir
        templates_dir = webgate_asset_dir() / "templates"
    except Exception:  # fail-open to the repo checkout
        from pathlib import Path
        templates_dir = Path(__file__).resolve().parent / "templates"
    return Jinja2Templates(directory=str(templates_dir))


_TEMPLATES = _templates()
_TEMPLATES.env.globals["console_display_name"] = webgate.console_display_name
_TEMPLATES.env.globals["branding"] = webgate.branding_config
_TEMPLATES.env.globals["get_version"] = get_version
# Posture default for the layout's tenant-nav block (P0-3) — same global as
# server.py's env; the explicit `is_multitenant` context var still wins.
_TEMPLATES.env.globals["is_multitenant_posture"] = webgate.is_multitenant


# --- shared helpers --------------------------------------------------------- #

def _data_dir() -> str:
    """Data home the running services use (``goals.db``/``cron.db``/``memory.db``).

    Honors ``POLYROB_DATA_DIR`` (the same env the agents/autonomy runtime read);
    defaults to ``data`` (the tool/runtime default).
    """
    return os.environ.get("POLYROB_DATA_DIR", "data")


def _cron_enabled() -> bool:
    """Whether the cron subsystem is on (lazy import to keep this module light)."""
    try:
        from tools.cronjob_tools import cron_enabled
        return cron_enabled()
    except Exception:
        return False


def _effective_user_id(request: Request) -> str:
    """Tenant identity for the webgate-v1 pages (memory/goals/cron/identity).

    Single-tenant postures (local/own_ops): ``webgate.local_owner_id()`` — there is
    exactly one owner and no separate caller-identity concept.
    Multitenant: the AUTHENTICATED caller's ``request.state.user_id`` (fixes
    assessment gap 5 — this used to be unconditionally ``local_owner_id()``,
    leaking the instance owner's memory/goals/cron/identity data to every
    authenticated tenant).

    Fails CLOSED, never open: if multitenant and ``request.state.user_id`` is
    missing/None/empty, this raises ``HTTPException(403)`` rather than falling
    back to ``local_owner_id()`` (the instance owner's data). ``auth_middleware``
    already 401s unauthenticated requests in multitenant mode before this is
    ever called, so the 403 here is defense-in-depth, not the expected path —
    but it must never resolve to the owner's identity.
    """
    if webgate.is_multitenant():
        uid = getattr(request.state, "user_id", None)
        if uid:
            return uid
        raise HTTPException(status_code=403, detail="Authenticated tenant identity required")
    return webgate.local_owner_id()


def _memory_provider():
    """The active external MemoryProvider, or None — reuse the registry/factory.

    Prefers a provider already registered by an agent in this process; otherwise
    constructs the configured backend read-only via the same factory the agent
    uses. Fail-open to None (Memory page then shows an empty state).
    """
    try:
        from modules.memory.registry import get_memory_registry
        active = get_memory_registry().active()
        if active is not None and getattr(active, "is_external", False):
            return active
    except Exception:
        pass
    try:
        from modules.memory.backend_factory import maybe_register_memory_backend
        return maybe_register_memory_backend(data_dir=_data_dir())
    except Exception:
        return None


def _split_snippets(raw: str) -> list:
    """Turn the provider's newline-joined ``- {content}`` string into a JSON list."""
    if not raw:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:]
        out.append(line)
    return out


def _goal_dict(g) -> dict:
    return {
        "id": g.id,
        "title": g.title,
        "body": g.body,
        "status": g.status,
        "priority": g.priority,
        "created_at": g.created_at,
        "completed_at": g.completed_at,
        "result": g.result,
    }


def _cron_dict(j) -> dict:
    return {
        "id": j.id,
        "task": j.task,
        "schedule_spec": j.schedule_spec,
        "next_run_at": j.next_run_at.isoformat() if j.next_run_at else None,
        "one_shot": j.one_shot,
        "enabled": j.enabled,
        "status": j.status,
        "last_run_at": j.last_run_at.isoformat() if getattr(j, "last_run_at", None) else None,
    }


def _version() -> str:
    # get_version() is the SSOT (source pyproject wins over stale installed
    # metadata). Reading importlib.metadata directly here re-introduced the stale-
    # install bug (a venv pinned at an old 1.0.0 would show 1.0.0). An explicit
    # WEBVIEW_VERSION override still wins for deployments that want to pin it.
    return os.environ.get("WEBVIEW_VERSION") or get_version()


def _provider_model():
    try:
        from cli.config_store import resolve_provider_model
        from modules.llm.profiles import PROFILES, providers_with_keys
        present = set(providers_with_keys(dict(os.environ)))
        keys = {p.env_key for p in PROFILES.values() if p.name in present}
        return resolve_provider_model(None, None, available_keys=keys)
    except Exception:
        return (None, None)


def _page_context(request: Request) -> dict:
    return {
        "request": request,
        "is_multitenant": webgate.is_multitenant(),
        "version": _version(),
        "ws_url": os.environ.get("WEBVIEW_WS_URL", ""),
    }


# --- JSON read endpoints ---------------------------------------------------- #

@router.get("/api/webgate/memory")
async def api_memory(request: Request, q: str = "", limit: int = 10):
    """Browse + search recall over the active MemoryProvider (tenant-scoped).

    ``q`` set → discover; ``q`` empty → browse-recent. Fail-open to no items.
    Returns ``{items, count, mode, limit}`` so the page can caption results
    honestly ("showing the N most recent" vs "N matches"). Per-hit
    timestamp/score provenance is NOT available from the provider's search
    surface (the ``memories`` FTS5 store has no timestamp column — the
    episodic-memory plan tracks that); the envelope carries what exists.
    """
    mode = "search" if (q or "").strip() else "browse"
    provider = _memory_provider()
    if provider is None:
        return JSONResponse({"items": [], "count": 0, "mode": mode, "limit": limit})
    user_id = _effective_user_id(request)  # raises 403 outside this try — must not be fail-open-swallowed
    try:
        raw = await provider.search(q or "", user_id=user_id, limit=limit)
    except Exception:
        return JSONResponse({"items": [], "count": 0, "mode": mode, "limit": limit})
    items = _split_snippets(raw)
    return JSONResponse({"items": items, "count": len(items), "mode": mode, "limit": limit})


@router.get("/api/webgate/goals")
async def api_goals(request: Request):
    """Durable goal board for the effective tenant — reuse ``GoalBoard.list()``."""
    if not AutonomyConfig.goals_enabled():
        return JSONResponse({"enabled": False, "goals": []})
    user_id = _effective_user_id(request)  # raises 403 outside this try — must not be fail-open-swallowed
    try:
        board = GoalBoard(os.path.join(_data_dir(), "goals.db"))
        goals = board.list(user_id=user_id)
        return JSONResponse({"enabled": True, "goals": [_goal_dict(g) for g in goals]})
    except Exception:
        return JSONResponse({"enabled": True, "goals": []})


@router.get("/api/webgate/cron")
async def api_cron(request: Request):
    """Cron job board for the effective tenant — reuse ``CronService.list_jobs()``."""
    if not _cron_enabled():
        return JSONResponse({"enabled": False, "jobs": []})
    user_id = _effective_user_id(request)  # raises 403 outside this try — must not be fail-open-swallowed
    try:
        service = CronService(CronJobStore(os.path.join(_data_dir(), "cron.db")))
        jobs = service.list_jobs(user_id=user_id)
        return JSONResponse({"enabled": True, "jobs": [_cron_dict(j) for j in jobs]})
    except Exception:
        return JSONResponse({"enabled": True, "jobs": []})


@router.get("/api/webgate/identity")
async def api_identity(request: Request):
    """SOUL/SELF identity — reuse ``core.instance`` (READ-ONLY, no write path).

    SOUL is deliberately NOT re-scoped (instance-wide by design,
    ``core/self_context_writer.py:25``); only the SELF-tier read
    (``load_self_doc``) is per-tenant.
    """
    home = _data_dir()
    owner = _effective_user_id(request)
    instance_id = resolve_instance_id()
    try:
        soul = load_self_context(home) or None
    except Exception:
        soul = None
    try:
        self_doc = load_self_doc(home, owner, instance_id) or None
    except Exception:
        self_doc = None
    return JSONResponse({
        "soul": soul,
        "self": self_doc,
        "instance_id": instance_id,
        "owner": owner,
    })


# --- avatar (pfp) — the instance's face rendered LIVE in the console ---------- #
# The engine is served same-origin so the console runs the EXACT avatar/mindprint.js
# on a canvas (animated), driven by the instance's frozen /pfp.json. All fail-open:
# no avatar yet -> 404 -> the identity page just shows a neutral fallback.
_AVATAR_DIR = Path(__file__).resolve().parents[1] / "avatar"


def _pfp_data_dir() -> str:
    """Data home to read the avatar from.

    The avatar is WRITTEN by the CLI (`pfp generate`), whose data home defaults to
    ``cwd/.polyrob`` when ``POLYROB_DATA_DIR`` is unset (``core.bootstrap.
    _resolve_cli_data_home``). Mirror that so generate↔serve agree in local dev; in
    prod (``POLYROB_DATA_DIR`` set) this is identical to :func:`_data_dir`.
    """
    env = os.environ.get("POLYROB_DATA_DIR")
    if env:
        return env
    local = Path.cwd() / ".polyrob"
    return str(local) if local.exists() else _data_dir()


@router.get("/pfp.json")
async def api_pfp_json():
    """The instance avatar identity blob (traits + engine-agnostic voice), or 404."""
    meta = load_pfp_meta(_pfp_data_dir(), resolve_instance_id())
    if not meta:
        return JSONResponse({"detail": "no avatar"}, status_code=404)
    return JSONResponse(meta)


@router.get("/pfp.png")
async def pfp_png():
    """The instance avatar still PNG (progressive fallback / OG image), or 404."""
    p = pfp_path(_pfp_data_dir(), resolve_instance_id())
    if not p.is_file():
        return Response(status_code=404)
    return FileResponse(str(p), media_type="image/png")


@router.get("/avatar/mindprint.js")
async def avatar_engine():
    """Serve the EXACT engine same-origin (classic script; sets window.Mindprint)."""
    p = _AVATAR_DIR / "mindprint.js"
    if not p.is_file():
        return Response(status_code=404)
    return FileResponse(str(p), media_type="application/javascript")


@router.get("/avatar/avatar-live.js")
async def avatar_live():
    """Serve the read-only live embed (fetch /pfp.json -> animate the canvas)."""
    p = _AVATAR_DIR / "webview" / "avatar-live.js"
    if not p.is_file():
        return Response(status_code=404)
    return FileResponse(str(p), media_type="application/javascript")


def _empty_ledger(user_id: str, days: int) -> dict:
    """The all-zero ledger shape — returned when the ledger read fails so the
    Finance page shows zeros rather than a 500 (every ledger leg fail-opens too)."""
    return {"user_id": user_id, "window_days": days, "llm_api_cost_usd": 0.0,
            "credits_spent": 0.0, "llm_calls": 0, "wallet_spend_usd": 0.0,
            "wallet_payments": 0, "earned_usd": 0.0, "settled_payments": 0,
            "pending_invoices_usd": 0.0, "pending_invoices": 0,
            "total_spend_usd": 0.0, "net_usd": 0.0}


def _ledger_caps() -> dict:
    """Display-only policy caps (NOT part of the ledger read model) — the operator
    context for the earned/spent/pending numbers. Resolved from env, fail-open."""
    def _f(name, default):
        try:
            v = os.environ.get(name)
            return float(v) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default
    return {
        "wallet_daily_cap_usd": _f("WALLET_DAILY_CAP_USD", None),
        "invoice_max_usd": _f("X402_INVOICE_MAX_USD", 50.0),
        "invoice_daily_max": _f("X402_INVOICE_DAILY_MAX", 10.0),
        "autonomy_budget_usd": _f("AUTONOMY_BUDGET_USD", 10.0),
    }


@router.get("/api/webgate/ledger")
async def api_ledger(request: Request, days: int = 7):
    """Unified financial ledger for the effective tenant — reuse ``build_ledger``.

    earned / spent / pending / net over a trailing window. Read-only, tenant-
    scoped (``_effective_user_id`` raises 403 outside the try — must not be
    fail-open-swallowed). All-zero-tolerant: a ledger error degrades to zeros."""
    days = max(1, min(int(days), 365))
    user_id = _effective_user_id(request)  # 403 in multitenant if no tenant identity
    try:
        ledger = await build_ledger(user_id, days=days)
    except Exception:
        ledger = _empty_ledger(user_id, days)
    ledger["caps"] = _ledger_caps()
    return JSONResponse(ledger)


@router.get("/finance", response_class=HTMLResponse)
async def finance_page(request: Request):
    return _TEMPLATES.TemplateResponse("finance.html", _page_context(request))


@router.get("/api/webgate/doctor")
async def api_doctor():
    """System health — reuse ``doctor_report`` (same checks as ``polyrob doctor``).

    The webview is a SERVER process — nothing does the CLI's POLYROB_LOCAL
    setdefault here — so both the checks and the header's ``memory_backend``
    resolve with ``absent_means_on=False`` (matching what
    ``modules.memory.backend_factory`` actually does at runtime). One shared
    resolution, so the page can never contradict itself again (P0-4).
    """
    env = dict(os.environ)
    try:
        checks = doctor_report(env, local_absent_means_on=False)
    except Exception:
        checks = []
    provider, model = _provider_model()
    rob_local = local_flag_on(env, absent_means_on=False)
    return JSONResponse({
        "checks": checks,
        "instance_id": resolve_instance_id(),
        "version": _version(),
        "provider": provider,
        "model": model,
        "memory_backend": resolve_memory_backend(env, rob_local),
    })


# --- page routes (render the template; data fetched client-side via the API) - #

@router.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request):
    return _TEMPLATES.TemplateResponse("memory.html", _page_context(request))


@router.get("/autonomy", response_class=HTMLResponse)
async def autonomy_page(request: Request):
    return _TEMPLATES.TemplateResponse("autonomy.html", _page_context(request))


@router.get("/identity", response_class=HTMLResponse)
async def identity_page(request: Request):
    return _TEMPLATES.TemplateResponse("identity.html", _page_context(request))


@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    return _TEMPLATES.TemplateResponse("system.html", _page_context(request))


__all__ = ["router"]
