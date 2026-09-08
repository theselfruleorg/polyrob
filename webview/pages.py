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
import logging
import asyncio
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
from core import self_evolution
from core.prefs import (
    PREF_SCHEMA,
    SENSITIVITY_GUARDED,
    display_effective,
    write_preference,
)
from core.surfaces import owner_admin
from cron.jobs import CronJobStore
from cron.service import CronService
from core.version import get_version
from modules.credits.unified_ledger import build_ledger, ledger_availability_note
# Owner goal-verb SSOT (030 C3): the SAME transition table + prefix resolver the
# Telegram `/goal` and `/settle` verbs use (surfaces/telegram/owner_ops.py) —
# never a second rulebook. The module is import-light (logging/os/typing only).
from surfaces.telegram import owner_ops

logger = logging.getLogger(__name__)

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
# Own-ops Logout visibility (030 S5) — same globals as server.py's env: the
# layout shows a Logout link for the authenticated own_ops owner, reading
# auth state straight from request.state (C4 contract).
from utils.auth_utils import is_authenticated as _request_is_authenticated  # noqa: E402
_TEMPLATES.env.globals["is_own_ops_posture"] = webgate.is_own_ops
_TEMPLATES.env.globals["request_is_authenticated"] = _request_is_authenticated


# --- shared helpers --------------------------------------------------------- #

def _data_dir() -> str:
    """Data home the running services use (``goals.db``/``cron.db``/``memory.db``).

    Delegates to :func:`webview.webgate.data_dir` — the webview wrapper over the
    ONE core policy seam ``core.runtime_paths.resolve_data_home`` shared with
    the CLI admin verbs (I-9, deduped 2026-07-12). Env wins; unset converges on
    the CLI/agent home; standalone deploy without ``core`` falls back to the
    legacy ``./data``. Keep this local name: routes and tests import it.
    """
    return webgate.data_dir()


def _cron_enabled_status() -> tuple:
    """``(enabled, error)`` — cron flag plus a short reason when the CHECK
    itself failed (import/read error), so "cron is off" and "the cron check
    broke" stop looking identical (030 D4). ``error`` is None on a clean read."""
    try:
        from tools.cronjob_tools import cron_enabled
        return bool(cron_enabled()), None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:200]


def _cron_enabled() -> bool:
    """Whether the cron subsystem is on (lazy import to keep this module light).

    Kept as the monkeypatch seam the routes/tests use; the reason-carrying
    variant is :func:`_cron_enabled_status`."""
    return _cron_enabled_status()[0]


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


def _memory_provider_status() -> tuple:
    """``(provider, error)`` — the active provider (or None) plus a short reason
    when backend CONSTRUCTION failed (030 D4). ``(None, None)`` means "no
    backend configured" (a genuine empty state); ``(None, reason)`` means the
    configured backend is broken and the page must say so instead of rendering
    an indistinguishable empty list."""
    try:
        from modules.memory.registry import get_memory_registry
        active = get_memory_registry().active()
        if active is not None and getattr(active, "is_external", False):
            return active, None
    except Exception:
        pass
    try:
        from modules.memory.backend_factory import maybe_register_memory_backend
        return maybe_register_memory_backend(data_dir=_data_dir()), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]


def _memory_provider():
    """The active external MemoryProvider, or None — reuse the registry/factory.

    Prefers a provider already registered by an agent in this process; otherwise
    constructs the configured backend read-only via the same factory the agent
    uses. Fail-open to None (Memory page then shows an empty state). Kept as
    the monkeypatch seam the routes/tests use; the reason-carrying variant is
    :func:`_memory_provider_status`.
    """
    return _memory_provider_status()[0]


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
        from modules.llm.profiles import PROFILES, providers_with_credentials
        # 024 L1.5: the credential oracle, not the key-only one — otherwise the
        # console reports "no provider" for a box running on a connected
        # account or a providers.yaml row.
        present = set(providers_with_credentials(dict(os.environ)))
        keys = {p.env_key for p in PROFILES.values() if p.name in present and p.env_key}
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
    honestly ("showing the N most recent" vs "N matches"). Since B2 (2026-07-12)
    provider rows stamped with write-time provenance render a leading
    ``[YYYY-MM-DD]`` inside the snippet text — deliberate: the date is the
    provenance the page used to lack. Legacy stampless rows stay bare.
    """
    mode = "search" if (q or "").strip() else "browse"
    provider = _memory_provider()
    if provider is None:
        return JSONResponse({"items": [], "count": 0, "mode": mode, "limit": limit})
    user_id = _effective_user_id(request)  # raises 403 outside this try — must not be fail-open-swallowed
    try:
        raw = await provider.search(q or "", user_id=user_id, limit=limit)
    except Exception as exc:
        # 030 D4: a failed recall read is NOT "no memories" — name it (still 200).
        return JSONResponse({"items": [], "count": 0, "mode": mode, "limit": limit,
                             "error": f"{type(exc).__name__}: {exc}"[:200]})
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
    except Exception as exc:
        # 030 D4: a failed read is NOT "no cron jobs" — name it (still 200).
        return JSONResponse({"enabled": True, "jobs": [],
                             "error": f"{type(exc).__name__}: {exc}"[:200]})


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


# --- avatar SETUP (web) — the same one-time draft→randomize→keep contract the CLI
# enforces. All three routes are 403 in read-only consoles, and the lock contract is
# enforced by modules/pfp/store (PfpLockedError) regardless of the caller — these
# routes surface it as {ok:false} rather than a 500.
def _pfp_setup_refused() -> None:
    if webgate.read_only():
        raise HTTPException(status_code=403, detail="read-only console")


@router.post("/api/pfp/generate")
async def api_pfp_generate():
    """Start setup: mint a RANDOM draft identity (no-op if an avatar already exists)."""
    _pfp_setup_refused()
    from modules.pfp import store
    from modules.pfp.identity import random_config
    home, instance_id = _pfp_data_dir(), resolve_instance_id()
    try:
        existing = load_pfp_meta(home, instance_id)
        if existing is not None:
            return JSONResponse({"ok": True, "meta": existing,
                                 "message": "avatar already exists"})
        meta = store.generate_pfp(home, instance_id, config=random_config())
        return JSONResponse({"ok": True, "meta": meta})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)})


@router.post("/api/pfp/randomize")
async def api_pfp_randomize(request: Request):
    """Re-roll the DRAFT (body: {"what": "all"|"face"|"voice"}). Refused once kept."""
    _pfp_setup_refused()
    from modules.pfp import store
    from modules.pfp.config import load_frozen_config
    from modules.pfp.identity import core_config, default_config, shuffle_face, shuffle_voice
    try:
        body = await request.json()
    except Exception:
        body = {}
    what = body.get("what") if body.get("what") in ("all", "face", "voice") else "all"
    home, instance_id = _pfp_data_dir(), resolve_instance_id()
    try:
        meta = load_pfp_meta(home, instance_id)
        if meta is not None and store.is_locked(meta):
            return JSONResponse({"ok": False,
                                 "message": "the identity is kept — setup happens once"})
        try:
            current = core_config(load_frozen_config(meta)) if meta else default_config()
        except Exception:
            current = default_config()
        if what == "voice":
            config = shuffle_voice(current)
        elif what == "face":
            config = shuffle_face(current)
        else:
            config = shuffle_face(current)
            config["override"].pop("voice", None)
        new_meta = store.generate_pfp(home, instance_id, config=config, force=True)
        return JSONResponse({"ok": True, "meta": new_meta})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)})


@router.post("/api/pfp/keep")
async def api_pfp_keep():
    """Accept the draft — lock the identity PERMANENTLY (one-way)."""
    _pfp_setup_refused()
    from modules.pfp import store
    home, instance_id = _pfp_data_dir(), resolve_instance_id()
    try:
        meta = store.keep_pfp(home, instance_id)
        return JSONResponse({"ok": True, "meta": meta})
    except FileNotFoundError:
        return JSONResponse({"ok": False, "message": "no avatar to keep — generate first"})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)})


def _empty_ledger(user_id: str, days: int) -> dict:
    """The all-zero ledger shape — returned when the ledger read fails so the
    Finance page shows zeros rather than a 500 (every ledger leg fail-opens too).

    SHAPE-IDENTICAL to a real ``build_ledger`` result (incl. the ``treasury``/
    ``runtime`` blocks) — otherwise the Finance page would silently render a
    different shape on the error path than on the happy path. Both blocks
    report ``available: False`` (this is the "we couldn't read it" path, not a
    genuine zero) and both balance fields are ``None`` (H14b: never a
    fabricated $0.00). No ``earned_usd``/``total_spend_usd``/top-level
    ``net_usd`` — those merged fields were deleted from ``build_ledger`` in
    Task 8 with no alias, and the fallback must stay shape-identical."""
    return {"user_id": user_id, "window_days": days,
            "llm_api_cost_usd": 0.0, "credits_spent": 0.0, "llm_calls": 0,
            "wallet_spend_usd": 0.0, "wallet_payments": 0, "settled_payments": 0,
            "pending_invoices_usd": 0.0, "pending_invoices": 0,
            "costs_available": False, "inbound_available": False,
            "wallet_metering": "error",
            "treasury": {"income_usd": 0.0, "spend_usd": 0.0, "pending_usd": 0.0,
                         "pending_count": 0, "balance_usd": None, "net_usd": 0.0,
                         "available": False},
            "runtime": {"spend_window_usd": 0.0, "spend_total_usd": 0.0,
                        "calls_window": 0, "calls_total": 0,
                        "provider_balance_usd": None, "available": False}}


def _wallet_daily_cap_display() -> dict:
    """Resolve WALLET_DAILY_CAP_USD's *display* state for the finance page —
    distinctly from an explicit disable AND from a malformed value (H3 fix
    round 1, 2026-08-22 review, Important 1).

    Before this fix, a single ``wallet_daily_cap_usd: None`` covered BOTH
    "explicitly disabled" (a deliberate posture — unbounded aggregate spend
    IS in force) and "malformed" (the real ``load_wallet_config()`` RAISES —
    the box is not running with ANY cap at all, a config error, not a
    posture). ``finance.html`` rendered both as the literal word "unset",
    which a reader could easily — and wrongly — read as "the $100 default
    must still be in force". They must never look the same, so this returns
    a distinct ``state`` alongside the amount:
      - ``"default"``      — unset/blank, the finite $100 default is in force
      - ``"explicit"``     — a real numeric value the operator set
      - ``"disabled"``     — an explicit sentinel (none/off/unlimited/disabled)
      - ``"misconfigured"``— malformed; ``load_wallet_config()`` would RAISE
    """
    from core.wallet.config import _cap_float, DEFAULT_DAILY_CAP_USD
    raw = os.environ.get("WALLET_DAILY_CAP_USD")
    is_unset = raw is None or not str(raw).strip()
    try:
        amount = _cap_float(os.environ, "WALLET_DAILY_CAP_USD", DEFAULT_DAILY_CAP_USD)
    except Exception as e:
        return {"amount": None, "state": "misconfigured", "error": str(e)}
    if amount is None:
        return {"amount": None, "state": "disabled", "error": None}
    return {"amount": amount, "state": "default" if is_unset else "explicit", "error": None}


def _ledger_caps() -> dict:
    """Display-only policy caps (NOT part of the ledger read model) — the operator
    context for the income/spend/pending numbers. Resolved from env, fail-open.

    NOTE: ``autonomy_budget_usd`` was removed — the budget gate it described no
    longer exists (see the money-ledger split proposal §5.3); this must never
    advertise a flag that's gone."""
    def _f(name, default):
        try:
            v = os.environ.get(name)
            return float(v) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default
    # H3 (2026-08-22): wallet_daily_cap_usd/_state delegate to the SAME parser
    # load_wallet_config() uses (never a second, divergent display parser).
    # _wallet_daily_cap_display() never raises (fail-open, this endpoint must
    # keep working); a genuinely unexpected error here still degrades to the
    # honest "misconfigured" state rather than silently claiming a cap.
    try:
        _daily = _wallet_daily_cap_display()
    except Exception:
        _daily = {"amount": None, "state": "misconfigured", "error": None}
    return {
        "wallet_daily_cap_usd": _daily["amount"],
        "wallet_daily_cap_state": _daily["state"],
        "invoice_max_usd": _f("X402_INVOICE_MAX_USD", 50.0),
        "invoice_daily_max": _f("X402_INVOICE_DAILY_MAX", 10.0),
    }


@router.get("/api/webgate/ledger")
async def api_ledger(request: Request, days: int = 7):
    """Unified financial ledger for the effective tenant — reuse ``build_ledger``.

    Two blocks that must NEVER be summed: ``treasury`` (the agent's own USDC —
    income/spend/pending/net) and ``runtime`` (the owner's LLM/API bill). This
    is a DISPLAY surface, so it opts into the two balance probes
    (``include_balances=True``). Read-only, tenant-scoped (``_effective_user_id``
    raises 403 outside the try — must not be fail-open-swallowed). All-zero-
    tolerant: a ledger error degrades to zeros (``_empty_ledger``, shape-
    identical to a real read)."""
    days = max(1, min(int(days), 365))
    user_id = _effective_user_id(request)  # 403 in multitenant if no tenant identity
    try:
        ledger = await build_ledger(user_id, days=days, include_balances=True)
    except Exception:
        ledger = _empty_ledger(user_id, days)
    ledger["caps"] = _ledger_caps()
    # H14b (final whole-branch review, Finding 1 — related root cause):
    # treasury/runtime carry `available` markers (and _empty_ledger's honest
    # `available: False`) with zero production readers — finance.html never
    # rendered them, so a broken read silently looked like a real $0.00 on
    # this surface too. `ledger_availability_note` is the SAME helper
    # core/recap.py and cli/ui/commands/h_finance.py already use; `None` when
    # every leg is available, so a healthy ledger gets no new key.
    ledger["note"] = ledger_availability_note(ledger)
    return JSONResponse(ledger)


@router.get("/finance", response_class=HTMLResponse)
async def finance_page(request: Request):
    ctx = _page_context(request)
    # 030 C3: the Invoices section renders a Settle action only on a writable
    # console — same read_only plumbing as /preferences and /pending.
    ctx["read_only"] = webgate.read_only()
    return _TEMPLATES.TemplateResponse(request, "finance.html", ctx)


@router.get("/api/webgate/positions")
async def api_positions(request: Request, chain: str = "base", ledger: str = ""):
    """The agent's on-chain BOOK — portfolio + ledger⟷chain reconcile (2026-08-27).

    The console showed money totals but never the positions behind them — the
    UI counterpart of the position-recall incident. Read-only: ``portfolio``
    and ``reconcile`` are read verbs (no signer, nothing broadcast). Honors
    ``DEFI_DATA_ENABLED``; every failure degrades to a LABELED unavailability,
    never a fabricated empty book. The ledger path is confined by the tool
    itself (data home / cwd only, credential files refused)."""
    _effective_user_id(request)  # 403 in multitenant if no tenant identity
    out = {"chain": chain, "enabled": None, "addresses": {}, "portfolio": None,
           "reconcile": None, "ledger_path": None, "error": None}
    try:
        from tools.defi import defi_data_enabled
        out["enabled"] = bool(defi_data_enabled())
    except Exception:
        out["enabled"] = None
    # Addresses are public information — show every identity the wallet holds,
    # Solana included, so the operator can see WHO the book belongs to.
    try:
        from core.wallet.factory import get_agent_wallet
        w = get_agent_wallet()
        if w is not None:
            try:
                out["addresses"]["evm"] = w.address
            except Exception:
                pass
            try:
                out["addresses"]["solana"] = w.solana_address
            except Exception:
                pass
    except Exception:
        pass
    if not out["enabled"]:
        out["error"] = ("DEFI_DATA_ENABLED is off — the book cannot be read "
                        "until an operator enables on-chain sight")
        return JSONResponse(out)
    try:
        from tools.defi.data_tool import (DefiDataTool, PortfolioParams,
                                          ReconcileParams)
        tool = DefiDataTool()
        pres = await tool.portfolio(PortfolioParams(chain=chain))
        out["portfolio"] = {"text": pres.extracted_content, "error": pres.error}
        lp = (ledger or "").strip()
        if not lp:
            try:
                from core.runtime_paths import resolve_data_home
                cand = (resolve_data_home() / "project"
                        / "kb-root-position-ledger.md")
                if cand.is_file():
                    lp = str(cand)
            except Exception:
                lp = ""
        if lp:
            out["ledger_path"] = lp
            rres = await tool.reconcile(
                ReconcileParams(chain=chain, ledger_path=lp))
            out["reconcile"] = {"text": rres.extracted_content,
                                "error": rres.error}
    except Exception as exc:
        out["error"] = f"positions read failed: {exc}"
    return JSONResponse(out)


@router.get("/positions", response_class=HTMLResponse)
async def positions_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "positions.html", _page_context(request))


@router.get("/api/webgate/config")
async def api_config_search(request: Request, query: str = ""):
    """018 P3: search/list BOTH config namespaces (prefs + ~409 catalog env
    flags) via core.config_service — the same brain every other surface uses.
    Read-only; secrets arrive pre-masked by the service."""
    user_id = _effective_user_id(request)
    from core import config_service
    items = []
    for info in config_service.search(query, user_id=user_id,
                                      home_dir=_data_dir(), limit=500):
        items.append({
            "key": info.key, "namespace": info.namespace, "kind": info.kind,
            "group": info.group, "description": info.description,
            "value": str(info.effective), "source": info.source,
            "applies": info.applies, "sensitivity": info.sensitivity,
            "enforcement": info.enforcement, "secret": info.secret,
            # legibility for the UI: which flags the console can never write
            # (otherwise discoverable only by PATCHing into a 403)
            "console_writable": info.key not in config_service.CONSOLE_UNWRITABLE_FLAGS,
        })
    return JSONResponse({"user_id": user_id, "settings": items})


@router.get("/api/webgate/config/{key}/explain")
async def api_config_explain(request: Request, key: str):
    """018 P3: full provenance chain for one setting (secrets masked)."""
    user_id = _effective_user_id(request)
    from core import config_service
    try:
        info = config_service.explain(key, user_id=user_id, home_dir=_data_dir())
    except KeyError:
        return JSONResponse({"error": f"unknown setting: {key}"}, status_code=404)
    return JSONResponse({
        "key": info.key, "namespace": info.namespace, "kind": info.kind,
        "value": str(info.effective), "source": info.source,
        "applies": info.applies, "sensitivity": info.sensitivity,
        "enforcement": info.enforcement, "secret": info.secret,
        "console_writable": info.key not in config_service.CONSOLE_UNWRITABLE_FLAGS,
        "description": info.description,
        "chain": [{"origin": s.origin, "value": str(s.value)} for s in info.chain],
    })


@router.patch("/api/webgate/config/{key}")
async def api_config_set(request: Request, key: str):
    """018 P3: write one setting through the config service.

    Prefs keep the EXACT preferences-PATCH trust ladder (guarded scalar needs
    ``confirm:true`` → the service queues otherwise); env-flag writes are
    additionally restricted to the local/own_ops postures (an authenticated
    OWNER console — never multitenant) and land in ./.polyrob/.env or
    ~/.polyrob/.env with the catalog shape-check, restart-effective."""
    if webgate.read_only():
        return JSONResponse({"error": "webview is read-only"}, status_code=403)
    user_id = _effective_user_id(request)
    from core import config_service
    from core.prefs import PREF_SCHEMA
    if key not in PREF_SCHEMA and webgate.posture() not in ("local", "own_ops"):
        return JSONResponse(
            {"error": "env-flag writes require the owner console "
                      "(local/own_ops posture)"}, status_code=403)
    # 024 §2.6: credential-equivalent flags (inference endpoint / credential
    # store selection) are never console-writable, at ANY posture — use the
    # local CLI (`polyrob config set …`).
    if key in config_service.CONSOLE_UNWRITABLE_FLAGS:
        return JSONResponse(
            {"error": f"'{key}' selects the agent's inference/credential "
                      "surface and is not writable from the console — set it "
                      "from the local CLI"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = config_service.set_value(
        key, str(body.get("value", "")),
        scope=body.get("scope"), user_id=user_id, home_dir=_data_dir(),
        confirm=bool(body.get("confirm")), surface="console")
    status = 200 if res.ok else 400
    if res.ok and res.outcome == "queued":
        status = 202
    return JSONResponse({"ok": res.ok, "outcome": res.outcome,
                         "message": res.message, "applies": res.applies},
                        status_code=status)


@router.get("/api/webgate/preferences")
async def api_preferences(request: Request):
    """Typed preferences, schema-driven (owner-UX P4 T3).

    One row per ``PREF_SCHEMA`` key with the spec fields the UI needs (type/
    sensitivity/applies/enum/range/description) plus the EFFECTIVE value and
    source via ``core.prefs.display_effective`` — the same helper the REPL
    ``/config`` and the agent-callable ``preferences`` action render with, so
    the console can never show state that drifts from enforcement. Tenant-
    scoped via ``_effective_user_id`` (403 in multitenant without identity)."""
    user_id = _effective_user_id(request)
    home = _data_dir()
    items = []
    for key, spec in PREF_SCHEMA.items():
        value, source = display_effective(key, user_id, home)
        items.append({
            "key": key,
            "description": spec.description,
            "type": spec.type,
            "sensitivity": spec.sensitivity,
            "merge": spec.merge,
            "applies": spec.applies,
            "env_flag": spec.env_flag,
            "enum_values": list(spec.enum_values),
            "min": spec.min_value,
            "max": spec.max_value,
            "value": value,
            "source": source,
        })
    return JSONResponse({"user_id": user_id, "preferences": items})


@router.patch("/api/webgate/preferences")
async def api_preferences_patch(request: Request):
    """Write one preference: ``{key, value, confirm?}`` (owner-UX P4 T3).

    SAFE keys apply immediately; GUARDED keys without ``confirm:true`` return
    409 ``{guarded: true}`` so the UI shows a confirm dialog; with
    ``confirm:true`` they apply directly — the authenticated PATCH IS the
    explicit owner confirmation, the same trust level as
    ``polyrob config set … --confirm`` (per the P4 plan: no double-approval).
    Validation/threat-scan/atomic-replace all live in
    ``core.prefs.write_preference`` — nothing is re-implemented here.
    ``WEBVIEW_READ_ONLY`` refuses with 403; tenant resolution is the same
    fail-closed ``_effective_user_id`` as GET (never writes as the owner).

    List-shrink routes through review (owner-UX P2-4 final review, item 3):
    the local webview posture has no auth, so a confirmed wholesale-replace of
    a GUARDED list key (``approvals.require``/``approvals.deny``) could
    silently DROP a pref-added gate — bypassing the ``remove_entry`` owner
    review flow every other surface enforces (``/approve remove``, the
    agent-callable ``preferences`` action). So for a list-typed guarded key,
    the new value is diffed against the CURRENT pref list: additions TIGHTEN
    policy and still apply directly (same trust level as any other guarded
    confirm); any REMOVED entry is instead queued as one
    ``propose_pref_change(op="remove_entry", ...)`` per entry (mirrors
    ``/approve remove`` — the owner reviews it via ``/pending``). A pure
    addition (no entry removed) or a scalar guarded key is unaffected and
    keeps the existing direct-apply, 200 response."""
    if webgate.read_only():
        raise HTTPException(status_code=403, detail="read-only console")
    user_id = _effective_user_id(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "JSON body must be an object"},
                            status_code=400)
    key = str(body.get("key") or "")
    spec = PREF_SCHEMA.get(key)
    if spec is None:
        return JSONResponse({"ok": False, "error": f"unknown preference: {key!r}"},
                            status_code=400)
    if "value" not in body or body["value"] is None:
        return JSONResponse({"ok": False, "error": "value is required"},
                            status_code=400)
    if spec.sensitivity == SENSITIVITY_GUARDED and body.get("confirm") is not True:
        return JSONResponse(
            {"ok": False, "guarded": True,
             "error": f"'{key}' is guarded — resend with confirm:true to apply"},
            status_code=409)
    if spec.sensitivity == SENSITIVITY_GUARDED and spec.type == "list":
        from core.prefs import load_preferences, propose_pref_change, validate_pref

        ok_new, new_items, verr = validate_pref(key, body.get("value"))
        if not ok_new:
            return JSONResponse({"ok": False, "error": verr}, status_code=400)
        current = list(load_preferences(_data_dir(), user_id).get(key, []) or [])
        removed = [item for item in current if item not in new_items]
        added = [item for item in new_items if item not in current]
        if removed:
            queued = []
            for entry in removed:
                ok_p, result = propose_pref_change(
                    user_id, key, None, _data_dir(), op="remove_entry", entry=entry)
                if ok_p:
                    queued.append({"key": key, "entry": entry, "proposal_id": result})
                else:
                    queued.append({"key": key, "entry": entry, "error": result})
            applied_additions: list = []
            if added:
                updated = sorted(set(current) | set(added))
                ok_add, add_err = write_preference(_data_dir(), user_id, key, updated)
                if ok_add:
                    applied_additions = added
            value, source = display_effective(key, user_id, _data_dir())
            return JSONResponse({
                "ok": True, "key": key, "queued": queued,
                "applied_additions": applied_additions,
                "value": value, "source": source,
            }, status_code=202)
        # Pure addition (or no-op) — falls through to the normal direct-apply.
    ok, err = write_preference(_data_dir(), user_id, key, body.get("value"))
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    value, source = display_effective(key, user_id, _data_dir())
    return JSONResponse({"ok": True, "key": key, "applies": spec.applies,
                         "value": value, "source": source})


def _pending_kwargs(request: Request) -> dict:
    """Shared (user_id, home_dir, instance_id) for the pending-review verbs —
    the SAME resolution `polyrob owner pending/promote/reject` uses, so a
    decision made in the console is byte-identical to one made on the CLI."""
    return {
        "user_id": _effective_user_id(request),
        "home_dir": _data_dir(),
        "instance_id": resolve_instance_id(),
    }


def _webgate_goal_board():
    """Same construction ``polyrob owner pending`` uses (``cli/commands/owner.py::
    _goal_board`` / ``api_goals`` above) — one ``goals.db`` under the data home."""
    return GoalBoard(os.path.join(_data_dir(), "goals.db"))


def _webgate_correspondent_registry():
    """Same construction ``polyrob owner pending/correspondents`` uses
    (``cli/commands/owner.py::_registry``) — one ``correspondents.db`` under the
    data home."""
    from core.surfaces.correspondents import CorrespondentRegistry
    return CorrespondentRegistry(os.path.join(_data_dir(), "correspondents.db"))


@router.get("/api/webgate/pending")
async def api_pending(request: Request):
    """The tenant's pending items: self-evolution proposals (identity/skills/
    contract/pref changes) via ``core.self_evolution``, PLUS (T10 parity with
    ``polyrob owner pending``) queued tool-approval asks
    (``tools.controller.approval_queue.list_pending_tool_approvals``) and
    pending correspondent bindings (``cli.commands.owner._pending_correspondent_items``
    — reused, not reimplemented). Read-only. Any one collector failing degrades
    to an empty contribution rather than a 500 for the whole aggregate."""
    kw = _pending_kwargs(request)
    items = self_evolution.list_pending(kw["user_id"], home_dir=kw["home_dir"],
                                        instance_id=kw["instance_id"])
    try:
        from tools.controller.approval_queue import list_pending_tool_approvals
        items = items + list_pending_tool_approvals(_webgate_goal_board(), kw["user_id"])
    except Exception:
        logger.debug("api_pending: tool-approval collector failed", exc_info=True)
    try:
        from cli.commands.owner import _pending_correspondent_items
        items = items + _pending_correspondent_items(_webgate_correspondent_registry(),
                                                      kw["user_id"])
    except Exception:
        logger.debug("api_pending: correspondent collector failed", exc_info=True)
    return JSONResponse({"user_id": kw["user_id"], "items": items})


@router.get("/api/webgate/pending/{kind}/{item_id}")
async def api_pending_show(request: Request, kind: str, item_id: str):
    """Full quarantined body of ONE proposal so promote/reject is an informed
    decision (same seam as `owner show-pending`)."""
    kw = _pending_kwargs(request)
    ok, body = self_evolution.show(kind, item_id, **kw)
    return JSONResponse({"ok": ok, "body": body})


def _decide_correspondent(item_id: str, user_id: str, *, approved: bool) -> tuple:
    """Route a correspondent pending-item decision through the SAME registry
    primitives every seat uses (``CorrespondentRegistry.approve``/``reject`` —
    never a reimplemented grant/deny path).

    ``item_id`` is ``"surface:address"`` (the id shape
    ``cli.commands.owner._pending_correspondent_items`` builds). Approve
    promotes ``pending -> active``; reject (030 C3 — the registry gained the
    decision verb) flips ``pending -> expired``: the contact stays as a
    tombstone so their replies remain unroutable and the idempotent seed path
    cannot silently re-open the same contact as a fresh pending row.
    """
    surface, sep, address = item_id.partition(":")
    if not sep or not surface or not address:
        return False, f"invalid correspondent id: {item_id!r} (expected surface:address)"
    registry = _webgate_correspondent_registry()
    if not approved:
        ok = registry.reject(surface=surface, address=address, user_id=user_id)
        if ok:
            return True, (f"rejected {surface}:{address} — their replies stay "
                          "unroutable")
        return False, f"no pending correspondent {surface}:{address}"
    ok = registry.approve(surface=surface, address=address, user_id=user_id)
    if ok:
        return True, f"approved {surface}:{address}"
    return False, f"no pending correspondent {surface}:{address}"


def _decide_pending(kind: str, item_id: str, kw: dict, *, approved: bool) -> tuple:
    """Dispatch a promote/reject decision to the RIGHT underlying function for
    ``kind`` (T10 parity with ``polyrob owner promote/reject`` — see
    ``cli/commands/owner.py``'s ``promote``/``reject`` commands, which this
    mirrors): ``tool_approval`` -> ``decide_tool_approval`` (the SAME function
    Telegram `/approve` `/reject` and the CLI call); ``correspondent`` ->
    :func:`_decide_correspondent`; anything else -> the existing
    ``core.self_evolution`` aggregator (identity/skills/contract/pref changes)."""
    if kind == "tool_approval":
        from tools.controller.approval_queue import decide_tool_approval
        return decide_tool_approval(_webgate_goal_board(), item_id,
                                    user_id=kw["user_id"], approved=approved)
    if kind == "correspondent":
        return _decide_correspondent(item_id, kw["user_id"], approved=approved)
    fn = self_evolution.promote if approved else self_evolution.reject
    return fn(kind, item_id, **kw)


@router.post("/api/webgate/pending/{kind}/{item_id}/promote")
async def api_pending_promote(request: Request, kind: str, item_id: str):
    """Owner decision: promote a pending proposal to active. 403 in read-only;
    tenant-scoped (a tenant can only act on its OWN queue). A miss/failure is
    ``{ok:false, message}``, never a 500 — the aggregator is the authority."""
    if webgate.read_only():
        raise HTTPException(status_code=403, detail="read-only console")
    kw = _pending_kwargs(request)
    ok, msg = _decide_pending(kind, item_id, kw, approved=True)
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/api/webgate/pending/{kind}/{item_id}/reject")
async def api_pending_reject(request: Request, kind: str, item_id: str):
    """Owner decision: reject (archive) a pending proposal. Same gating as
    promote."""
    if webgate.read_only():
        raise HTTPException(status_code=403, detail="read-only console")
    kw = _pending_kwargs(request)
    ok, msg = _decide_pending(kind, item_id, kw, approved=False)
    return JSONResponse({"ok": ok, "message": msg})


# --------------------------------------------------------------------------- #
# Owner control plane (030 WS-C C3): kill switch · goal/cron writes ·
# invoices + settle. Every verb is thin plumbing over the SAME primitives the
# CLI (`polyrob owner …`), the REPL (`/halt` …) and Telegram call — never a
# second implementation of the rule. Every mutation mirrors the existing
# write-route gating exactly: WEBVIEW_READ_ONLY refuses with 403 and tenant
# identity comes from the same fail-closed ``_effective_user_id``.
# --------------------------------------------------------------------------- #

#: The webview's goal write verbs (subset of the Telegram `/goal` table —
#: exactly the four the C3 decision granted).
_WEBGATE_GOAL_VERBS = ("pause", "resume", "retry", "cancel")

#: Valid invoice status filters (same set as the CLI/REPL/Telegram listings).
_INVOICE_STATUSES = ("pending", "completed", "expired")


def _mutation_refused() -> None:
    """Shared WEBVIEW_READ_ONLY gate for the C3 write verbs — the EXACT check
    the preferences PATCH and pending promote/reject use (403, same detail)."""
    if webgate.read_only():
        raise HTTPException(status_code=403, detail="read-only console")


def _owner_console_required() -> None:
    """Instance-wide controls (halt/resume) additionally require the OWNER
    console — local/own_ops posture — mirroring the env-flag write rule in
    ``api_config_set``: an authenticated multitenant TENANT must never halt
    (or resume) the whole instance's autonomy."""
    if webgate.posture() not in ("local", "own_ops"):
        raise HTTPException(
            status_code=403,
            detail="the pause controls require the owner console "
                   "(local/own_ops posture)")


def _pause_state_json(st) -> dict:
    """The 031 pause record as the page renders it. ``halted`` / ``env_halt``
    are kept for the old panel/scripts (halt == pause of everything)."""
    out = st.to_dict()
    out.update({"halted": bool(st.paused and "all" in st.scopes),
                "env_halt": owner_admin.env_halt_active()})
    return out


@router.get("/api/webgate/pause")
async def api_pause_state():
    """The live pause state — read through the ONE record (031, fail-closed),
    so the page can never show a state the runtime does not enforce."""
    return JSONResponse(_pause_state_json(owner_admin.pause_state(_data_dir())))


@router.get("/api/webgate/halt")
async def api_halt_state():
    """Alias kept for the old panel/scripts: halt == pause(all)."""
    return JSONResponse(_pause_state_json(owner_admin.pause_state(_data_dir())))


async def _do_pause(request: Request, scopes, duration_minutes):
    _mutation_refused()
    _owner_console_required()
    _effective_user_id(request)  # 403 in multitenant without identity
    try:
        minutes = int(duration_minutes) if duration_minutes not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="duration_minutes must be an integer")
    if minutes is not None and minutes < 1:
        raise HTTPException(status_code=400, detail="duration_minutes must be at least 1")
    try:
        from core.surfaces.owner_intent import parse_pause_args
        sc, _ = parse_pause_args(list(scopes or ()))  # same validation as /pause
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    res = owner_admin.pause_autonomy(_data_dir(), scopes=sc, duration_minutes=minutes,
                                     reason="paused from the web console", via="webview")
    body = _pause_state_json(res.state)
    body.update({"ok": res.effective, "effective": res.effective, "written": res.written,
                 "message": owner_admin.render_pause_result(
                     res, resume_hint="the Resume button", status_hint="This page", chat=False),
                 "state": _pause_state_json(res.state)})
    return JSONResponse(body)


@router.post("/api/webgate/pause")
async def api_pause(request: Request):
    """Pause autonomous work (031): body ``{scopes?: [...], duration_minutes?: int}``.

    Same seam as `polyrob autonomy pause`, the REPL `/pause` and Telegram
    `/pause` (``core.surfaces.owner_admin.pause_autonomy``) — the message is the
    VERIFIED (read-back) state, never a green checkmark on a write."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    scopes = body.get("scopes") or ()
    if not isinstance(scopes, (list, tuple)) or not all(isinstance(x, str) for x in scopes):
        raise HTTPException(status_code=400, detail="scopes must be a list of scope names")
    return await _do_pause(request, tuple(scopes), body.get("duration_minutes"))


@router.post("/api/webgate/halt")
async def api_halt(request: Request):
    """Alias of ``POST /api/webgate/pause`` with no body: pause everything."""
    return await _do_pause(request, ("all",), None)


@router.post("/api/webgate/resume")
async def api_resume(request: Request):
    """Lift the pause (everything, or ``{scopes: [...]}``) — mirrors
    `polyrob autonomy resume` / REPL `/resume` / Telegram `/resume`.

    Honest verified-state reporting: never claims RESUMED while the runtime
    still reports a pause (e.g. ``AUTONOMY_HALT`` set in the environment, which
    only an env-file edit + restart can clear)."""
    _mutation_refused()
    _owner_console_required()
    _effective_user_id(request)  # 403 in multitenant without identity
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    scopes = body.get("scopes") or None
    if scopes is not None and (not isinstance(scopes, (list, tuple))
                               or not all(isinstance(x, str) for x in scopes)):
        raise HTTPException(status_code=400, detail="scopes must be a list of scope names")
    if scopes:
        try:
            from core.surfaces.owner_intent import parse_pause_args
            scopes, _ = parse_pause_args(list(scopes))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    res = owner_admin.resume_autonomy_scopes(
        _data_dir(), scopes=None if (not scopes or "all" in scopes) else tuple(scopes),
        via="webview")
    body = _pause_state_json(res.state)
    # `still_halted` = any scope still paused (the resume verb's honest answer);
    # `halted` (from _pause_state_json) = the `all` scope, on every response.
    body.update({"ok": res.effective, "removed": res.removed, "written": res.written,
                 "message": owner_admin.render_resume_result(res, halt_hint="the Pause buttons"),
                 "state": _pause_state_json(res.state), "still_halted": res.state.paused})
    return JSONResponse(body)


@router.post("/api/webgate/goals/{goal_id}/{verb}")
async def api_goal_verb(request: Request, goal_id: str, verb: str):
    """Per-goal owner write verb (030 C3, owner decision Q2 = yes) — the same
    ``GoalBoard.update_status`` transitions Telegram's `/goal` uses, via the
    SAME transition table (``surfaces.telegram.owner_ops._GOAL_TRANSITIONS``).

    ⚠️ ``board.get()`` is NOT tenant-scoped — the id is resolved within the
    CALLER's own goals and the write passes ``user_id=``, exactly as
    ``owner_ops.goal_reply`` does. A goal id belonging to another tenant is a
    404 miss, never a cross-tenant mutation."""
    _mutation_refused()
    if verb not in _WEBGATE_GOAL_VERBS:
        return JSONResponse(
            {"ok": False,
             "message": f"unknown goal verb: {verb} "
                        f"(one of {', '.join(_WEBGATE_GOAL_VERBS)})"},
            status_code=400)
    user_id = _effective_user_id(request)
    from agents.task.goals.board import KIND_GOAL
    board = _webgate_goal_board()
    mine = [g for g in board.list(user_id=user_id, limit=1000)
            if g.kind == KIND_GOAL]
    goal = next((g for g in mine if g.id == goal_id), None)
    if goal is None:
        return JSONResponse(
            {"ok": False, "message": f"no goal {goal_id} for this tenant"},
            status_code=404)
    target, reset, allowed, participle = owner_ops._GOAL_TRANSITIONS[verb]
    if allowed is not None and goal.status not in allowed:
        return JSONResponse(
            {"ok": False,
             "message": (f"goal is {goal.status} — only {'/'.join(allowed)} "
                         f"goals can be {participle}")},
            status_code=409)
    if goal.status == target:
        return JSONResponse({"ok": True, "status": target,
                             "message": f"already {target}"})
    warning = None
    if verb == "pause" and goal.status == "running":
        warning = ("it is running right now — the pause takes effect after "
                   "this run")
    ok = board.update_status(goal_id, target, reset_failures=reset,
                             user_id=user_id)
    if not ok:
        return JSONResponse(
            {"ok": False, "message": f"could not update {goal_id}"},
            status_code=409)
    suffix = " (failures cleared)" if reset else ""
    return JSONResponse({"ok": True, "status": target,
                         "message": f"goal → {target}{suffix}",
                         "warning": warning})


@router.post("/api/webgate/cron/{job_id}/cancel")
async def api_cron_cancel(request: Request, job_id: str):
    """Cancel a cron job — mirrors Telegram `/cron cancel` (``CronService.
    cancel`` with ``user_id=``). The id is resolved within the caller's OWN
    jobs first, so another tenant's job id is a 404 miss."""
    _mutation_refused()
    user_id = _effective_user_id(request)
    service = CronService(CronJobStore(os.path.join(_data_dir(), "cron.db")))
    mine = {j.id for j in service.list_jobs(user_id=user_id)}
    if job_id not in mine:
        return JSONResponse(
            {"ok": False, "message": f"no cron job {job_id} for this tenant"},
            status_code=404)
    ok = service.cancel(job_id, user_id=user_id)
    if not ok:
        return JSONResponse(
            {"ok": False, "message": f"could not cancel {job_id}"},
            status_code=409)
    return JSONResponse({"ok": True, "message": f"cancelled {job_id}"})


async def _with_owner_money_db(coro_factory):
    """``(ok, result_or_msg)`` over the right money DB (invoices live in
    ``bot.db``, not the webgate sqlite stores).

    The REPL's resolution (``cli/ui/commands/h_owner.py::_run_owner_db``):
    prefer a live container's ``database_manager`` (the same rows a running
    agent writes); with none — the normal webview shape, a separate process —
    fall back to the CLI's own bot.db resolution
    (``cli.commands.owner._with_bot_db`` — DB_PATH env wins, else the
    data-home candidates), which reports "no bot.db found" honestly instead
    of a container bootstrap error."""
    db = None
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()  # raises if never built
        db = container.get_service("database_manager")
    except Exception:
        db = None
    if db is not None:
        return True, await coro_factory(db)
    from cli.commands.owner import _with_bot_db
    return await _with_bot_db(coro_factory)


def _invoicing_off_note():
    """The honest feature-off note (same grammar as the CLI/REPL invoice
    listings), or None while the settlement watcher is enabled. A resolver
    error counts as OFF (fail-to-warn, matching ``warn_if_flag_off``)."""
    try:
        from modules.x402.invoicing import x402_invoicing_enabled
        if x402_invoicing_enabled():
            return None
    except Exception:
        pass
    return ("X402_INVOICE_ENABLED is off — rows are durable, but no "
            "settlement watcher runs; pending invoices will not settle or "
            "wake sessions.")


@router.get("/api/webgate/invoices")
async def api_invoices(request: Request, status: str = ""):
    """Tenant-scoped invoice listing — reuse ``modules.x402.invoicing.
    list_payment_requests`` (the exact seam `polyrob owner invoices --user`
    and the REPL `/invoices` read). A failed read is NOT "no invoices" — it
    carries an ``error`` field (030 D4)."""
    user_id = _effective_user_id(request)
    status_f = (status or "").strip().lower() or None
    if status_f is not None and status_f not in _INVOICE_STATUSES:
        return JSONResponse(
            {"error": f"status must be one of {', '.join(_INVOICE_STATUSES)}"},
            status_code=400)

    async def _list(db):
        from modules.x402.invoicing import list_payment_requests
        return await list_payment_requests(user_id=user_id, status=status_f,
                                           limit=50, db=db)

    try:
        ok, rows = await _with_owner_money_db(_list)
    except Exception as exc:
        return JSONResponse({"user_id": user_id, "invoices": [], "count": 0,
                             "error": f"{type(exc).__name__}: {exc}"[:200]})
    if not ok:
        return JSONResponse({"user_id": user_id, "invoices": [], "count": 0,
                             "error": str(rows)})
    return JSONResponse({"user_id": user_id, "invoices": rows,
                         "count": len(rows), "note": _invoicing_off_note()})


@router.post("/api/webgate/invoices/{request_id}/settle")
async def api_invoice_settle(request: Request, request_id: str):
    """Attest an invoice as PAID (pending → completed) — owner attestation,
    not a payment. Same primitive as `polyrob owner settle <id> [--tx-hash]`
    and the REPL `/settle` (``settle_payment_request``), with Telegram
    `/settle`'s tenant scoping: the id is resolved among the CALLER's OWN
    pending invoices first (via ``owner_ops.resolve_prefix``, so the short
    ids listings show are accepted), so one tenant can never attest another
    tenant's row. Optional JSON body: ``{"tx_hash": "0x…"}``."""
    _mutation_refused()
    user_id = _effective_user_id(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    tx_hash = (body.get("tx_hash") or None) if isinstance(body, dict) else None

    async def _settle(db):
        from modules.x402.invoicing import (list_payment_requests,
                                            settle_payment_request)
        rows = await list_payment_requests(user_id=user_id, status="pending",
                                           limit=200, db=db)
        resolved, err = owner_ops.resolve_prefix(
            request_id, [str(r["request_id"]) for r in rows])
        if err:
            return {"ok": False, "status_code": 404,
                    "message": f"{err} among this tenant's PENDING invoices"}
        settled = await settle_payment_request(resolved,
                                               transaction_hash=tx_hash, db=db)
        if not settled:
            return {"ok": False, "status_code": 409,
                    "message": (f"{resolved} was not settled — it may already "
                                "be completed, or that tx hash already "
                                "settled another invoice")}
        return {"ok": True, "status_code": 200, "request_id": resolved,
                "message": f"settled {resolved}"}

    try:
        ok, out = await _with_owner_money_db(_settle)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "message": f"settle failed: {exc}"[:300]},
            status_code=500)
    if not ok:
        return JSONResponse({"ok": False, "message": str(out)},
                            status_code=503)
    status_code = out.pop("status_code", 200)
    out["note"] = _invoicing_off_note()
    return JSONResponse(out, status_code=status_code)


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
    # 2026-08-28 status SSOT: the same snapshot Telegram /status renders, for
    # the instance owner (the console's single principal on the local/own_ops
    # postures). A builder failure is reported, never an empty "healthy".
    health: dict
    status_lines: list = []
    try:
        from core.status_snapshot import build_status_snapshot
        from core.status_render import render_health_lines, render_status_lines
        owner = webgate.local_owner_id()
        snap = await asyncio.to_thread(build_status_snapshot, owner, data_dir=_data_dir(),
                                       include_money=True)
        health = {"overall": snap.overall,
                  "items": [{"key": h.key, "severity": h.severity, "text": h.text,
                             "remedy": h.remedy} for h in snap.health],
                  "unverified": list(snap.unavailable_sources),
                  "lines": render_health_lines(snap, prefix="")}
        status_lines = render_status_lines(snap, prefix="")
    except Exception as e:
        health = {"overall": "unavailable", "items": [], "unverified": [],
                  "lines": [f"Health: unavailable ({type(e).__name__}: {str(e)[:120]})"]}
    return JSONResponse({
        "checks": checks,
        "health": health,
        "status_lines": status_lines,
        "instance_id": resolve_instance_id(),
        "version": _version(),
        "provider": provider,
        "model": model,
        "memory_backend": resolve_memory_backend(env, rob_local),
    })


# --- page routes (render the template; data fetched client-side via the API) - #

@router.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request):
    ctx = _page_context(request)
    # 030 D4: a broken memory backend must not render as an empty list — the
    # template shows "memory backend unavailable: <reason>" when this is set.
    ctx["memory_error"] = _memory_provider_status()[1]
    return _TEMPLATES.TemplateResponse(request, "memory.html", ctx)


@router.get("/autonomy", response_class=HTMLResponse)
async def autonomy_page(request: Request):
    ctx = _page_context(request)
    # 030 D4: a broken cron CHECK must not render as "cron disabled".
    ctx["cron_error"] = _cron_enabled_status()[1]
    # 030 C3: kill switch + per-row goal/cron actions render only on a
    # writable console; the halt controls additionally need the owner console
    # (local/own_ops — the same posture rule the POST endpoints enforce), so
    # the page never shows a button the API would refuse.
    ctx["read_only"] = webgate.read_only()
    ctx["halt_controls"] = (not webgate.read_only()
                            and webgate.posture() in ("local", "own_ops"))
    return _TEMPLATES.TemplateResponse(request, "autonomy.html", ctx)


@router.get("/identity", response_class=HTMLResponse)
async def identity_page(request: Request):
    """Read-only Identity page. ``ui.show_avatar`` (owner-UX prefs) decides whether
    the avatar block renders at all — resolved for the page's effective tenant
    (same seam the identity JSON endpoint uses), fail-open to True on ANY
    resolution error (missing prefs module, unsafe/absent tenant id, disk error)."""
    ctx = _page_context(request)
    show_avatar = True
    try:
        from core import prefs as _prefs
        user_id = _effective_user_id(request)
        show_avatar = bool(_prefs.resolve("ui.show_avatar", user_id, _data_dir(),
                                          env_value=None, default=True))
    except Exception:
        show_avatar = True
    ctx["show_avatar"] = show_avatar
    ctx["read_only"] = webgate.read_only()
    return _TEMPLATES.TemplateResponse(request, "identity.html", ctx)


@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "system.html", _page_context(request))


@router.get("/preferences", response_class=HTMLResponse)
async def preferences_page(request: Request):
    ctx = _page_context(request)
    ctx["read_only"] = webgate.read_only()
    return _TEMPLATES.TemplateResponse(request, "preferences.html", ctx)


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """030 C3 (018 P3 residue): the UI over the three already-shipped
    ``/api/webgate/config*`` endpoints — searchable flag+pref list with
    explain-on-click and editing per the API's own rules. ``flags_writable``
    mirrors the PATCH endpoint's env-flag posture rule so the page never
    offers an edit the server would 403 (prefs stay editable everywhere the
    console is writable)."""
    ctx = _page_context(request)
    ctx["read_only"] = webgate.read_only()
    ctx["flags_writable"] = (not webgate.read_only()
                             and webgate.posture() in ("local", "own_ops"))
    return _TEMPLATES.TemplateResponse(request, "config.html", ctx)


@router.get("/pending", response_class=HTMLResponse)
async def pending_page(request: Request):
    ctx = _page_context(request)
    ctx["read_only"] = webgate.read_only()
    return _TEMPLATES.TemplateResponse(request, "pending.html", ctx)


__all__ = ["router"]
