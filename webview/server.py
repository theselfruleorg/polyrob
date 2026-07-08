from __future__ import annotations

"""WebView ASGI server for POLYROB sessions.

This tiny server fulfils three roles:

1. Serve the static front-end assets and Jinja2 templates that make up the
   Web-UI shipped in the *webview* package.
2. Expose a very small HTTP JSON API that the UI can call to get the initial
   state of a session (feed entries, workspace tree…).
3. Provide a **WebSocket / Socket.IO** real-time stream so browsers can receive
   live updates as soon as the POLYROB agents append new telemetry to the *feed*
   directory of the session.

The implementation purposefully avoids any heavy frameworks – *FastAPI* +
*python-socketio* + *watchfiles* give us everything we need while keeping the
runtime footprint minimal.
"""

from pathlib import Path
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
import os
from datetime import datetime
import time
import hashlib
import hmac
from collections import defaultdict

import socketio
from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from watchfiles import awatch, Change

# Local utility that aggregates session statistics from feed files
from webview.stats_service import compute_session_stats

# Webgate config object — single source of truth for single-user (default) vs
# multitenant mode. Consulted at the four seam points below (middleware, ownership,
# page/router mount-gate). Read at IMPORT time for the mount-gate; at REQUEST time
# for the middleware/ownership short-circuits.
from webview import webgate

# Bounded-collection utility (LRU eviction) — reused for _event_emissions (E5-Minor)
# so the rate-limiter's per-session key space can't grow without bound.
from utils.bounded_collections import BoundedDict

# No demo mode - WebView must use the real PathManager
import os
import sys

# NOTE: a legacy `sys.path.insert(0, '/opt/rob')` used to live here (pre-rename
# install path). On any box where the stale /opt/rob tree still exists it
# HIJACKED every `agents`/`modules`/`core` import away from the live tree —
# removed 2026-07-06. The webview imports from the tree it is deployed in
# (WorkingDirectory/PYTHONPATH), never a hardcoded absolute path.

# Import PathManager for session paths
from agents.task.path import pm

from core.version import get_version

logger = logging.getLogger("webview.server")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

FEED_DEFAULT_LIMIT = max(1, int(os.environ.get('WEBVIEW_FEED_DEFAULT_LIMIT', '500')))
FEED_MAX_LIMIT = max(1, int(os.environ.get('WEBVIEW_FEED_MAX_LIMIT', '2000')))

logger.info(f"Feed event limits: default={FEED_DEFAULT_LIMIT}, max={FEED_MAX_LIMIT}")

# SECURITY FIX: Get allowed origins from environment
# Include the deploy's domain by default to prevent Socket.IO connection failures
# One domain SSOT with api/auth_endpoints.py's SIWE `domain` (which already reads
# WEBVIEW_DOMAIN) — an operator overrides one env var, not two independent hardcodes.
_webview_domain = os.environ.get("WEBVIEW_DOMAIN", "localhost:3000").strip()


def _compute_cors_origins() -> List[str]:
    """Explicit CORS_ALLOW_ORIGINS wins verbatim; otherwise the default list:
    legacy localhost:3000 entries + WEBVIEW_DOMAIN + the console's own serving
    origins (bind port on localhost/127.0.0.1) — the webview serves its own UI,
    so the serving origin must be allowed or the browser's same-origin
    Socket.IO handshake is rejected with a 400 (P0-1, 2026-07-06)."""
    raw = os.environ.get("CORS_ALLOW_ORIGINS", "").strip()
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    origins = [
        "http://localhost:3000",
        "https://localhost:3000",
        f"https://{_webview_domain}",
        f"http://{_webview_domain}",
    ]
    port = webgate.bind_port()
    for scheme in ("http", "https"):
        for host in ("localhost", "127.0.0.1"):
            origins.append(f"{scheme}://{host}:{port}")
    return list(dict.fromkeys(origins))


_cors_origins = _compute_cors_origins()
logger.info(f"Socket.IO CORS allowed origins: {_cors_origins}")


def _cors_origin_allowed(origin: Optional[str], environ: Optional[dict] = None) -> bool:
    """engineio ``cors_allowed_origins`` callable: allow the explicit list OR a
    TRUE same-origin request (Origin == scheme://Host).

    Host is a browser-forbidden request header, so a cross-origin attacker page
    cannot make Origin match it. X-Forwarded-Host/-Proto ARE settable from
    cross-origin JS — they must never feed this comparison (only the scheme may
    vary, which concedes nothing a network MITM doesn't already have).
    """
    if origin in _cors_origins:
        return True
    host = (environ or {}).get("HTTP_HOST")
    if not origin or not host:
        return False
    return origin in (f"http://{host}", f"https://{host}")


# Socket.IO instance that will be used for live updates
_sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=_cors_origin_allowed,  # SECURITY: allowlist + true same-origin only
    max_http_buffer_size=2_000_000,  # 2 MB limit to prevent memory spikes
    ping_timeout=60,  # Ping timeout to detect disconnected clients
    ping_interval=25,  # Ping interval for keep-alive
    engineio_logger=False,  # Disable verbose logging
    logger=False  # Disable Socket.IO debug logging
)

# Initialize FastAPI with a specific title
_fastapi = FastAPI(title=f"{webgate.console_display_name()} API")


def _posture_route(method: str, path: str, postures: tuple, **kwargs):
    """`@_fastapi.<method>`-equivalent decorator that ONLY registers the route
    when the CURRENT posture (``webgate.posture()``) is one of ``postures``.

    Generalizes the old binary ``_multitenant_get`` to the full 3-posture
    model (B4). The handler functions stay in this file unchanged (00 §6.5:
    demote behind the flag, never delete) — only their *registration* is
    gated. Read at import time (the route table is built once).
    """
    def deco(fn):
        if webgate.posture() in postures:
            return getattr(_fastapi, method)(path, **kwargs)(fn)
        return fn
    return deco


def _posture_get(path: str, postures: tuple = ("multitenant",), **kwargs):
    """`@_fastapi.get`-equivalent decorator, gated by posture (default:
    multitenant-only, matching the old `_multitenant_get` default)."""
    return _posture_route("get", path, postures, **kwargs)


def _posture_post(path: str, postures: tuple = ("multitenant",), **kwargs):
    """`@_fastapi.post`-equivalent decorator, gated by posture."""
    return _posture_route("post", path, postures, **kwargs)


def _multitenant_get(path: str, **kwargs):
    """Back-compat alias — multitenant-only routes (signin/logout/profile/admin).

    The signin/logout/profile/admin pages are the multitenant surface — in
    other postures they are simply not registered (a request → 404; that
    surface does not exist there).
    """
    return _posture_get(path, postures=("multitenant",), **kwargs)


# Global references for services
_container = None
_wallet_generator: Optional[Any] = None

# Process start time, for the public /api/status uptime figure (B2).
_START_TIME = time.monotonic()


@_fastapi.get("/api/status")
async def api_status(request: Request) -> Response:
    """Public, always-reachable status JSON — no posture gate, no auth.

    Mirrors api/app.py's /health shape in spirit (this is the webview process,
    a separate service in a typical deployment). Consumed client-side by
    status.html.
    """
    from core.instance import resolve_instance_id

    return JSONResponse({
        "status": "live",
        "instance": resolve_instance_id(),
        "version": os.environ.get("WEBVIEW_VERSION", get_version()),
        "uptime_seconds": int(time.monotonic() - _START_TIME),
    })


@_fastapi.on_event("startup")
async def startup_event():
    """Initialize all services before accepting requests."""
    global _container, _wallet_generator

    logger.info("🚀 Initializing webview services...")

    # 0. Install the process-global session data root BEFORE anything touches
    # pm() (RC-1, 2026-07-07): the agent process derives its tree from
    # POLYROB_DATA_DIR via build_cli_container; without this the webview's
    # pm() falls back to env DATA_ROOT (./data/task) and silently browses a
    # DIFFERENT (stale) tree. Same SSOT pattern the CLI uses. Fail-open with a
    # loud error — a path issue must not take the console down, but it must
    # never be silent again either.
    try:
        from core.runtime_paths import resolve_session_data_root
        from agents.task.path import get_path_manager, set_path_manager
        _session_root = resolve_session_data_root()
        set_path_manager(get_path_manager(data_root=str(_session_root)))
        logger.info(f"✅ Session data root: {_session_root}")
    except Exception as e:
        logger.error(f"❌ Failed to install session data root — pm() will use "
                     f"its legacy default and may browse the WRONG tree: {e}",
                     exc_info=True)

    # 1. Validate critical environment variables.
    # JWT is only used by the multitenant auth layer; the single-user webgate
    # (WEBGATE_MULTITENANT=OFF, the default) has no auth at all, so requiring a
    # JWT secret it never uses would needlessly block the loopback primitive.
    if webgate.is_multitenant():
        jwt_secret = os.environ.get("JWT_SECRET_KEY")
        if not jwt_secret:
            raise RuntimeError("❌ JWT_SECRET_KEY not configured - cannot start service")
        logger.info("✅ JWT authentication configured")
    else:
        logger.info("✅ webgate single-user mode: JWT not required (no auth)")

    # 2. Initialize DependencyContainer and core services
    try:
        from core.container import DependencyContainer
        from core.config import BotConfig
        from core.initialization import initialize_core

        # Create config first (required for container initialization)
        config = BotConfig()
        _container = DependencyContainer.get_instance(config=config)
        logger.info("✅ DependencyContainer initialized")

        # Initialize core (database, memory, cache)
        # This is all webview needs - auth services are in polyrob-api.service!
        await initialize_core(_container)
        logger.info("✅ Core services initialized (database, memory, cache)")

        # ✅ ARCHITECTURE FIX:
        # Webview does NOT initialize auth services (balance_manager, tier_manager, etc.)
        # Those are in polyrob-api.service (port 9000) - frontend calls /api/payments/* which nginx routes there
        # Webview only needs: database (read sessions), memory (session context), cache (performance)

        logger.info("🎉 Webview services initialized - UI server ready!")
        logger.info("💡 Payment/Auth APIs handled by polyrob-api.service (port 9000) via nginx proxy")

    except Exception as e:
        logger.error(f"❌ Failed to initialize services: {e}", exc_info=True)
        raise RuntimeError(f"Cannot start webview without services: {e}")

    # Auth/task/wallet half (was a second, competing startup handler).
    await _startup_late_services()

# Security headers middleware
@_fastapi.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)

    # Check if this is a /serve/ endpoint (allows iframe embedding for presentations)
    is_serve_endpoint = "/workspace/serve/" in str(request.url.path)

    if is_serve_endpoint:
        # Relaxed CSP for iframe-embedded content (presentations, HTML apps)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' https:; "
            "script-src 'self' 'unsafe-inline' https:; "
            "style-src 'self' 'unsafe-inline' https:; "
            "font-src 'self' https: data:; "
            "img-src 'self' data: https: blob:; "
            "connect-src 'self' https:; "
            "frame-src 'self' blob: data: https:; "
            "frame-ancestors 'self'; "  # Allow embedding by same origin
            "base-uri 'self'; "
            "form-action 'self'"
        )
        # No X-Frame-Options for serve endpoint (allows iframe embedding)
    else:
        # Content Security Policy - strict but allows needed resources
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.socket.io https://cdnjs.cloudflare.com https://fonts.googleapis.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https: blob:; "
            "connect-src 'self' ws: wss: https://cdn.socket.io https://cdnjs.cloudflare.com; "
            "frame-src 'self' blob: data:; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers["X-Frame-Options"] = "DENY"

    # Security headers (apply to all)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    # Cache control for sensitive pages
    if "/session/" in str(request.url):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"

    return response

# Global exception handler to prevent information leakage
@_fastapi.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions without leaking sensitive information."""
    # Log the full error server-side
    logger.error(f"Unhandled exception: {exc}", exc_info=True)

    # Return generic error to client
    return JSONResponse(
        status_code=500,
        content={"error": "An internal error occurred. Please try again later."}
    )

# Add static files - need to add this BEFORE creating ASGI app
# Resolve the webgate asset base via core.assets (packaged web_dist bundle when
# built, else the repo webview/ checkout — byte-identical in a dev tree).
try:
    from core.assets import webgate_asset_dir as _webgate_asset_dir
    STATIC_DIR = _webgate_asset_dir() / "static"
except Exception:  # fail-open to the legacy repo-relative path
    STATIC_DIR = Path(__file__).resolve().parent / "static"

# Middleware to add cache-control headers for JS files
@_fastapi.middleware("http")
async def add_cache_control_headers(request: Request, call_next):
    """Add no-cache headers for JS files to prevent stale cache issues."""
    response = await call_next(request)
    if request.url.path.startswith("/static/js/") and request.url.path.endswith(".js"):
        # Force revalidation for JS files
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

_fastapi.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static"
)

# Final app for Uvicorn to run - order is important here
app = socketio.ASGIApp(_sio, other_asgi_app=_fastapi)

# For tracking watchers and client sessions
_watch_tasks: dict[str, asyncio.Task] = {}
_client_session: dict[str, str] = {}
_session_clients: dict[str, int] = {}

from collections import defaultdict

# Rate limiting configuration (hardcoded for webview - not business logic)
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_CONNECTIONS = 10  # max connections per IP per window
RATE_LIMIT_MAX_EVENTS = 100  # max events per session per window
# Cap on distinct sessions tracked by the event-rate limiter (E5-Minor): without
# this, _event_emissions grew one key per session forever (a slow leak) since
# nothing ever popped a finished session's entry. LRU eviction bounds the key
# space without changing rate-limit semantics for any session still active.
EVENT_EMISSIONS_MAX_SESSIONS = 5000

# Track connection attempts by IP
_connection_attempts: dict[str, list[float]] = defaultdict(list)
# Track event emissions by session (bounded — LRU-evicted past EVENT_EMISSIONS_MAX_SESSIONS)
_event_emissions: "BoundedDict[str, list[float]]" = BoundedDict(max_size=EVENT_EMISSIONS_MAX_SESSIONS)

def check_rate_limit(ip: str) -> bool:
    """Check if IP has exceeded connection rate limit.

    Args:
        ip: IP address to check

    Returns:
        True if within limit, False if exceeded
    """
    now = time.time()

    # Clean old attempts
    _connection_attempts[ip] = [
        t for t in _connection_attempts[ip]
        if now - t < RATE_LIMIT_WINDOW
    ]

    # Check limit
    if len(_connection_attempts[ip]) >= RATE_LIMIT_MAX_CONNECTIONS:
        logger.warning(f"Rate limit exceeded for IP {ip}: {len(_connection_attempts[ip])} connections in {RATE_LIMIT_WINDOW}s")
        return False

    # Record this attempt
    _connection_attempts[ip].append(now)
    return True

def check_event_rate_limit(session_id: str) -> bool:
    """Check if session has exceeded event emission rate limit.

    Args:
        session_id: Session ID to check

    Returns:
        True if within limit, False if exceeded
    """
    now = time.time()

    # Clean old events. _event_emissions is a bounded (LRU-evicted) dict — read
    # via .get(default=[]) rather than defaultdict auto-vivification, then write
    # back explicitly so the write also refreshes the key's LRU position.
    events = [
        t for t in _event_emissions.get(session_id, [])
        if now - t < RATE_LIMIT_WINDOW
    ]
    _event_emissions[session_id] = events

    # Check limit
    if len(events) >= RATE_LIMIT_MAX_EVENTS:
        logger.warning(f"Event rate limit exceeded for session {session_id}: {len(events)} events in {RATE_LIMIT_WINDOW}s")
        return False

    # Record this event
    events.append(now)
    return True


async def _emit_feed_event(entry: dict, room: str) -> bool:
    """Emit one feed_update event to `room`, honoring the per-session event rate
    limit (E5). Returns False (and drops the event) if the session has exceeded
    RATE_LIMIT_MAX_EVENTS within RATE_LIMIT_WINDOW — closes the gap where
    check_event_rate_limit was defined but never called.

    Fail-safe: if the rate-limit check itself raises, we log and fall through to
    emitting the event rather than crashing the feed watcher's loop.
    """
    try:
        allowed = check_event_rate_limit(room)
    except Exception as exc:
        logger.error("check_event_rate_limit raised for room %s: %s", room, exc, exc_info=True)
        allowed = True

    if not allowed:
        logger.warning("Dropping feed_update for %s: event rate limit exceeded", room)
        return False

    await _sio.emit("feed_update", entry, room=room)
    return True


def _enrich_llm_event_with_cost(entry: dict) -> dict:
    """Enrich LLM request event with cost estimate if missing.

    Uses the centralized cost calculation from stats_service which relies on
    the model registry for consistent, up-to-date pricing across the application.

    Args:
        entry: Feed event dictionary (must have type='llm_request')

    Returns:
        The same entry dict (modified in place), with cost_estimate added if missing
    """
    if entry.get("type") != "llm_request" or "data" not in entry:
        return entry

    data = entry["data"]

    # Check if cost_estimate is missing or zero
    cost_estimate = data.get("cost_estimate")
    if cost_estimate is None or cost_estimate == 0:
        # Import centralized cost calculation from stats_service
        try:
            from webview.stats_service import _calculate_cost_from_registry
            
            # Extract token counts
            prompt_tokens = data.get("prompt_tokens")
            completion_tokens = data.get("completion_tokens")
            total_tokens = data.get("token_count") or data.get("total_tokens")
            model_name = data.get("model_name")
            
            # Calculate using centralized registry (handles all models)
            estimated_cost = _calculate_cost_from_registry(
                model_name=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens
            )
            
            if estimated_cost > 0:
                data["cost_estimate"] = estimated_cost
                
        except Exception as e:
            # If cost calculation fails, log but don't crash
            logger.debug(f"Cost calculation failed: {e}")
            data["cost_estimate"] = 0.0

    return entry

async def get_clean_session_id(session_id: str) -> str:
    """Clean a session ID and return it. Use as a FastAPI dependency."""
    return pm().clean_session_id(session_id)



def _manual_auth_check(request: Request) -> None:
    """Manually validate JWT token and populate request.state for public endpoints.
    
    Args:
        request: FastAPI request object
        
    Side Effects:
        Populates request.state with user info if valid token found
        
    Note:
        This is needed for endpoints in public_paths where auth middleware doesn't run.
        For protected endpoints, use the normal auth middleware instead.
    """
    # Posture "local": loopback operator IS the owner, there is no JWT/SIWE/owner
    # login, ever — nothing to decode. own_ops/multitenant both mint tokens
    # (owner-login cookie or wallet/SIWE JWT respectively) that must be
    # decode-able here too — this is what makes the own_ops "owner logs in →
    # dashboard" round-trip work (B4).
    if not webgate.requires_owner_login():
        return

    auth_token = None
    auth_header = request.headers.get("Authorization")

    if auth_header and auth_header.startswith("Bearer "):
        auth_token = auth_header[7:]
    else:
        auth_token = request.cookies.get("auth_token")

    # Validate and populate request.state if token exists
    if auth_token:
        try:
            import jwt as pyjwt
            jwt_secret = os.environ.get("JWT_SECRET_KEY")

            if jwt_secret:
                decoded = pyjwt.decode(auth_token, jwt_secret, algorithms=["HS256"])
                from api.auth_state import set_auth_state
                request.state.wallet_address = decoded.get("sub")
                set_auth_state(
                    request.state,
                    user_id=decoded.get("user_id"),
                    tier=decoded.get("tier", "free"),
                    role=decoded.get("role", "user"),
                    payment_method=decoded.get("payment_method"),
                    authenticated=True,
                )
                logger.debug(f"Manual auth successful: {request.state.user_id}")
        except Exception as e:
            logger.debug(f"Manual auth failed: {e}")
            # Don't populate request.state - user treated as unauthenticated


def _check_session_ownership(request: Request, session_id: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Check if the current user owns a session.
    
    Args:
        request: FastAPI request with auth state
        session_id: The session ID to check
        
    Returns:
        Tuple of (is_owner, current_user_id, session_owner_id)
        
    Note:
        This is the centralized ownership check used across all endpoints.
        Returns (False, None, None) if user is not authenticated.
        Returns (False, user_id, owner_id) if authenticated but not owner.
        Returns (True, user_id, owner_id) if authenticated and is owner.
    """
    # Single-user webgate (posture "local"): every session is owned by the local
    # owner. No 401/403 on interaction, no JWT — the loopback operator IS the
    # owner. Gated on requires_owner_login() (False only for "local"), NOT
    # is_multitenant() (B4-M) — own_ops has real auth (owner-login cookie) too,
    # so it must run the actual ownership check below rather than this bypass.
    if not webgate.requires_owner_login():
        local_owner = webgate.local_owner_id()
        return (True, local_owner, local_owner)

    from utils.auth_utils import get_authenticated_user_id, is_authenticated

    # Check authentication
    if not is_authenticated(request):
        owner_id = pm().get_session_user(session_id)
        return (False, None, owner_id)
    
    # Get current user
    current_user_id = get_authenticated_user_id(request)

    # Get session owner
    session_owner_id = pm().get_session_user(session_id)

    if webgate.is_own_ops():
        # own_ops has exactly ONE owner: the authenticated owner-login identity
        # (upstream route auth already ensures only the owner reaches a
        # protected route in the first place). That owner owns EVERY session
        # in this instance, regardless of which surface/identity path tagged
        # it — e.g. CLI-created sessions are hardcoded to user_id="local"
        # (core/identity.py), which never equals the own_ops owner-login id
        # (webgate.local_owner_id(), default "rob"). A strict per-session
        # string match here false-denies the owner on their own CLI sessions.
        # So: authenticated-as-owner -> allow unconditionally; anything else
        # (authenticated as someone/something else) -> deny. This keeps H2b's
        # real security value (a non-owner identity is still denied) without
        # the CLI-session false-deny.
        is_owner = bool(current_user_id) and current_user_id == webgate.local_owner_id()
        if is_owner:
            return (True, current_user_id, session_owner_id or current_user_id)
        return (False, current_user_id, session_owner_id)

    # multitenant: multiple tenants share this instance, so per-session
    # ownership must be checked strictly.
    is_owner = (current_user_id == session_owner_id) if session_owner_id else False

    return (is_owner, current_user_id, session_owner_id)

ROOT_DIR = Path(__file__).resolve().parent
# Templates resolve through the same webgate asset base as STATIC_DIR above
# (packaged web_dist bundle when built, else the repo webview/ checkout).
try:
    from core.assets import webgate_asset_dir as _webgate_asset_dir
    TEMPLATES_DIR = _webgate_asset_dir() / "templates"
except Exception:  # fail-open to the legacy repo-relative path
    TEMPLATES_DIR = ROOT_DIR / "templates"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# UI branding (Workstream D): registered as Jinja globals so every template
# can render the current console name / footer links without every route
# threading them through its own context dict.
_templates.env.globals["console_display_name"] = webgate.console_display_name
_templates.env.globals["branding"] = webgate.branding_config
_templates.env.globals["get_version"] = get_version
# Posture default for the layout's tenant-nav block (P0-3): pages that don't
# pass `is_multitenant` fall back to the posture SSOT instead of "shown".
_templates.env.globals["is_multitenant_posture"] = webgate.is_multitenant

# Multitenant-only: the wallet auth router is the JWT/SIWE surface. In single-user
# mode it is simply not mounted (no /api/auth/* — single-user has no auth).
if webgate.is_multitenant():
    try:
        from api.auth_endpoints import router as auth_router
        _fastapi.include_router(auth_router, prefix="/api/auth", tags=["authentication"])
        AUTH_ROUTER_MOUNTED = True
        logger.info("✅ Auth endpoints mounted at /api/auth (nonce, verify, me)")
    except ImportError as e:
        AUTH_ROUTER_MOUNTED = False
        logger.error(f"❌ Failed to mount auth router: {e}")
        logger.warning("⚠️ Wallet authentication will not work!")
else:
    AUTH_ROUTER_MOUNTED = False
    logger.info("webgate single-user mode: auth router not mounted (no JWT/SIWE)")

async def _task_router_read_only_guard(request: Request) -> None:
    """WS-3.2 (2026-07-07): the task router is mounted DIRECTLY in this app,
    so its mutating routes (POST /api/task/sessions, …/messages, …/cancel)
    would bypass the wrapper endpoints' webgate.read_only() checks. Refuse
    mutations at the router seam in read-only consoles; reads stay allowed."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and webgate.read_only():
        raise HTTPException(
            status_code=403,
            detail="Console is read-only (WEBVIEW_READ_ONLY)",
        )


try:
    from api.task_http_api import router as task_router
    _fastapi.include_router(
        task_router, prefix="/api", tags=["task"],
        dependencies=[Depends(_task_router_read_only_guard)],
    )
    TASK_ROUTER_MOUNTED = True
    logger.info("✅ Task endpoints mounted at /api/task")
except ImportError as e:
    TASK_ROUTER_MOUNTED = False
    logger.error(f"❌ Failed to mount task router: {e}")
    logger.warning("⚠️ Task session creation from webview will not work!")

PAYMENT_ROUTER_MOUNTED = False

# Webgate v1 read-only pages (Memory/Autonomy/Identity/System) — core single-user
# surfaces. Mounted in ALL postures; per-tenant scoping under multitenant is
# handled INSIDE each handler via webview.pages._effective_user_id (B7 —
# closes assessment gap 5, was previously hardcoded to the instance owner in
# every posture). Each endpoint REUSES the underlying service (memory
# provider / GoalBoard / CronService / core.instance / doctor_report); see
# webview/pages.py. Fail-open: a mount failure must never break the webview boot.
try:
    from webview.pages import router as webgate_pages_router
    _fastapi.include_router(webgate_pages_router)
    PAGES_ROUTER_MOUNTED = True
    logger.info("✅ Webgate v1 pages mounted (memory/autonomy/identity/system)")
except Exception as e:
    PAGES_ROUTER_MOUNTED = False
    logger.error(f"❌ Failed to mount webgate pages router: {e}")

# Global activity stream (/activity page + /api/activity/*). Mounted in ALL
# postures; access is enforced at request time inside webview/activity.py
# (_require_activity_access): local open, own_ops behind the owner cookie
# (auth middleware — /activity is deliberately NOT a public path), multitenant
# admin/instance-owner only. Fail-open mount: a failure never breaks boot.
try:
    from webview.activity import router as activity_router
    _fastapi.include_router(activity_router)
    ACTIVITY_ROUTER_MOUNTED = True
    logger.info("✅ Activity stream mounted (/activity)")
except Exception as e:
    ACTIVITY_ROUTER_MOUNTED = False
    logger.error(f"❌ Failed to mount activity router: {e}")

def _login_redirect_path() -> str:
    """Where an unauthenticated browser request should be sent to authenticate.

    own_ops has no ``/signin`` (that surface is multitenant-only, gated by
    ``_multitenant_get``) — send it to ``/owner-login`` instead. Multitenant
    keeps its existing ``/signin`` target unchanged.
    """
    return "/owner-login" if webgate.is_own_ops() else "/signin"


@_fastapi.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Require authentication (owner login or wallet/SIWE) for all routes except
    signin/owner-login and static files. Runs for BOTH own_ops and multitenant
    postures (B4) — only "local" (the loopback operator IS the owner) skips it.
    """

    # Posture "local" (WEBGATE_MULTITENANT=OFF and no POLYROB_POSTURE override,
    # the default): no JWT/SIWE/owner-login, ever — fold into the auth-disabled
    # short-circuit. own_ops/multitenant both require SOME authenticated
    # identity (webgate.requires_owner_login()) and run the checks below.
    if not webgate.requires_owner_login():
        # The loopback operator IS the owner (the same statement
        # _check_session_ownership's local bypass makes). Stamp the canonical
        # owner auth state so downstream gates that read request.state — the
        # task router's payment admin-bypass, catalog scope, template
        # is_authenticated branches — see the owner instead of an anonymous
        # user; without this the local console 402s on POST /api/task/sessions
        # (WS-3 E2E finding, 2026-07-07).
        try:
            from api.auth_state import set_auth_state
            set_auth_state(
                request.state,
                user_id=webgate.local_owner_id(),
                tier="admin",
                role="owner",
                payment_method=None,
                authenticated=True,
            )
        except Exception:
            pass
        return await call_next(request)

    # Check if auth is enabled (default: enabled in production, disabled in dev)
    env = os.environ.get("ENV", "production")
    auth_enabled_default = "false" if env == "development" else "true"
    auth_enabled = os.environ.get("WEBVIEW_AUTH_ENABLED", auth_enabled_default).lower() == "true"

    # If auth is disabled, skip all checks
    if not auth_enabled:
        logger.debug("Auth disabled - allowing request")
        return await call_next(request)

    path = request.url.path

    # Public paths (no auth required)
    # Note: /api/auth/me requires auth, so use specific paths for public auth endpoints
    public_paths = [
        "/signin",
        "/owner-login",
        "/logout",
        "/static",
        "/api/status",
        "/api/auth/nonce",
        "/api/auth/verify",
        "/api/payments/pricing",  # Only pricing info is public (no user data)
        "/api/webview/sessions/",  # Internal streaming from agent (localhost only, verified in endpoint)
    ]
    # Shareable-link viewing (/session/, /api/session/ — which also covers the
    # workspace-file/screenshot sub-routes) stays public ONLY in the
    # "multitenant" posture, matching the original shareable-link product
    # intent (design predates the posture model, when "multitenant" was the
    # only public posture that existed). own_ops has NO public session-viewing
    # surface at all — there is only one owner, so "shareable" (implying
    # sharing with someone who ISN'T the owner) has no identity model to hang
    # off of — closing gaps 2/3 from the alignment assessment. "local" never
    # reaches this middleware body (short-circuited above), so this branch is
    # unreachable there and Posture 0 is unaffected.
    if webgate.posture() == "multitenant":
        public_paths += ["/session/", "/api/session/"]
    # SECURITY ARCHITECTURE:
    # - Session viewing endpoints are public ONLY in multitenant (shareable links)
    # - Session interaction (POST messages) is protected by ownership check in endpoint
    # - Ownership verification happens in send_message_to_session (401/403 responses)
    # - User data isolation maintained via user_id in _get_user_sessions
    #
    # `/` (exact match, NOT a prefix — a prefix would exempt every path) is the
    # B2 posture-aware root: it must always be reachable and does its OWN
    # is_authenticated()-branch (status page vs dashboard) in the handler, so
    # it is let through here too (after populating request.state via
    # _manual_auth_check, same as the other public paths) rather than
    # redirected to a login page by this middleware.
    if path == "/" or any(path.startswith(p) for p in public_paths):
        # Attempt to authenticate if token present (for better UX)
        # This allows detecting session owners vs viewers
        _manual_auth_check(request)
        return await call_next(request)

    # Check for auth token in header or cookie
    auth_header = request.headers.get("Authorization")
    auth_token = None

    # Get all cookies for auth check (debug logging removed for security)

    if auth_header and auth_header.startswith("Bearer "):
        auth_token = auth_header[7:]
        logger.info(f"🔐 Found token in Authorization header for {path}")
    else:
        # Check cookie
        auth_token = request.cookies.get("auth_token")
        if auth_token:
            logger.info(f"🔐 Found token in cookie for {path} (token length: {len(auth_token)})")
        else:
            logger.info(f"🔐 No token found in cookie or header for {path}")
            logger.info(f"   Cookies received: {list(request.cookies.keys())}")

    if not auth_token:
        # No token - redirect to the posture-appropriate login page with a
        # return_to parameter. A real HTTP redirect (not a 200 JS-hack page)
        # so this is provably a denial, not a served response — safe-by-default.
        login_path = _login_redirect_path()
        logger.info(f"❌ No auth token found - redirecting to {login_path} (path={path})")
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"error": "Authentication required"})
        from urllib.parse import quote
        return_to = quote(path)
        return RedirectResponse(url=f"{login_path}?return_to={return_to}", status_code=302)

    # Verify JWT token properly
    jwt_secret = os.environ.get("JWT_SECRET_KEY")
    if not jwt_secret:
        logger.error("JWT_SECRET_KEY not configured")
        if path.startswith("/api/"):
            return JSONResponse(status_code=500, content={"error": "Auth not configured"})
        response = RedirectResponse(url=_login_redirect_path(), status_code=302)
        response.delete_cookie("auth_token")
        return response

    # SECURITY FIX: Never log JWT secret, even partially - removed debug logging

    try:
        import jwt as pyjwt
        decoded = pyjwt.decode(auth_token, jwt_secret, algorithms=["HS256"])

        # Token is valid - extract user info and role
        # NEW JWT STRUCTURE: "sub" = wallet_address, "user_id" = internal ID
        # request.state is populated via the canonical C4 contract
        # (api/auth_state.py::set_auth_state) so an owner-login token
        # (webview/owner_auth.py, role="owner") converges on the exact same
        # request.state shape a wallet/SIWE token does.
        from api.auth_state import set_auth_state
        request.state.wallet_address = decoded.get("sub")  # Wallet is now primary!
        request.state.chain = decoded.get("chain", "ethereum")
        set_auth_state(
            request.state,
            user_id=decoded.get("user_id"),     # Internal DB ID
            tier=decoded.get("tier", "free"),
            role=decoded.get("role", "user"),
            payment_method=decoded.get("payment_method"),
            authenticated=True,
        )

        # Admin check using centralized auth_constants (single source of truth)
        from api.auth_constants import is_admin as check_admin
        request.state.is_admin = check_admin(
            role=request.state.role,
            wallet_address=request.state.wallet_address
        )

        if request.state.is_admin:
            logger.info(f"🔐 Webview admin access granted: {request.state.wallet_address} (role: {request.state.role})")

        logger.info(f"✅ Webview auth successful: user={request.state.user_id}, tier={request.state.tier}, admin={request.state.is_admin}")

    except pyjwt.ExpiredSignatureError:
        logger.warning(f"❌ Token expired for path={path}")
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"error": "Token expired"})
        from urllib.parse import quote
        return_to = quote(path)
        response = RedirectResponse(url=f"{_login_redirect_path()}?return_to={return_to}", status_code=302)
        response.delete_cookie("auth_token")  # Clear invalid token
        return response

    except pyjwt.InvalidTokenError as e:
        logger.error(f"❌ Invalid token for path={path}: {e}")
        logger.error(f"   Token (first 50 chars): {auth_token[:50] if auth_token else 'None'}")
        logger.error(f"   JWT_SECRET_KEY present: {bool(jwt_secret)}")
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"error": "Invalid token"})
        from urllib.parse import quote
        return_to = quote(path)
        response = RedirectResponse(url=f"{_login_redirect_path()}?return_to={return_to}", status_code=302)
        response.delete_cookie("auth_token")  # Clear invalid token
        return response

    response = await call_next(request)
    return response


def _collect_sessions_in_dir(user_path, user_label: str) -> List[Dict[str, Any]]:
    """Collect session rows from ONE user directory.

    Rows keep the raw ``created_timestamp`` so callers can sort ACROSS user
    dirs before stripping it; each row carries ``user`` (the directory name)
    so the catalog can label who a session belongs to.
    """
    sessions: List[Dict[str, Any]] = []
    if not user_path.exists():
        logger.debug(f"User path does not exist: {user_path}")
        return sessions

    # Sessions are stored at: {data_root}/{user_id}/{session_id}/
    for session_path in user_path.iterdir():
        if not session_path.is_dir():
            continue

        feed_dir = session_path / "feed"
        if not feed_dir.exists():
            continue

        # Read task and metadata
        task_text = "No task description"
        model = None
        provider = None
        status = "completed"

        # Read task.json
        task_file = session_path / "task.json"
        if task_file.exists():
            try:
                with task_file.open('r') as f:
                    task_data = json.load(f)
                    task_text = task_data.get('task', task_text)
                    model = task_data.get('model')
                    provider = task_data.get('provider')
            except Exception as e:
                logger.debug(f"Failed to read task.json: {e}")

        # Read status.json for current status
        status_file = session_path / "status.json"
        if status_file.exists():
            try:
                with status_file.open('r') as f:
                    status_data = json.load(f)
                    status = status_data.get('status', 'completed')
            except Exception as e:
                logger.debug(f"Failed to read status.json: {e}")

        # Fallback to metadata.json
        if task_text == "No task description":
            metadata_file = session_path / "metadata.json"
            if metadata_file.exists():
                try:
                    with metadata_file.open('r') as f:
                        metadata = json.load(f)
                        task_text = metadata.get('task', task_text)
                        if not model:
                            model = metadata.get('model')
                        if not provider:
                            provider = metadata.get('provider')
                except Exception as e:
                    logger.debug(f"Failed to read metadata.json: {e}")

        # Get creation time
        created_timestamp = session_path.stat().st_ctime
        created = datetime.fromtimestamp(created_timestamp)

        # Count steps
        step_files = list(feed_dir.glob('step_*.json')) + list(feed_dir.glob('agent_step_*.json'))

        sessions.append({
            'id': session_path.name,
            'user': user_label,
            'task': task_text[:100],
            'created': created.strftime('%Y-%m-%d %H:%M'),
            'created_timestamp': created_timestamp,
            'steps': len(step_files),
            'status': status,
            'model': model or 'unknown',
            'provider': provider or 'unknown'
        })

    return sessions


def _finalize_session_rows(sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort newest-first across whatever dirs the rows came from, then strip
    the temporary sort key."""
    sessions.sort(key=lambda s: s['created_timestamp'], reverse=True)
    for session in sessions:
        session.pop('created_timestamp', None)
    return sessions


def _get_user_sessions(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load sessions with rich metadata for a specific user.

    Args:
        user_id: User ID to fetch sessions for. If None, uses DEFAULT_USER_ID.

    Returns:
        List of session metadata dicts

    Security:
        Only returns sessions for the specified user - multi-user isolation enforced.
    """
    sessions = []
    try:
        # Get data directory from PathManager
        data_root = pm().data_root

        logger.info(f"Loading sessions from data_root: {data_root}")

        # Default to DEFAULT_USER_ID if no user_id provided
        if not user_id:
            from agents.task.constants import DEFAULT_USER_ID
            user_id = DEFAULT_USER_ID

        # BACKWARD COMPATIBILITY: Check both old (cleaned) and new (proper) user directories
        # This handles sessions created before the clean_user_id() fix
        potential_user_dirs = [user_id]

        # For _anonymous_ user, also check the old "anonymous" directory (cleaned version)
        if user_id == "_anonymous_":
            potential_user_dirs.append("anonymous")  # Old PathManager cleaned this
            logger.info("Checking backward compatibility path for _anonymous_ → anonymous")

        # Collect sessions from all potential user directories
        for check_user_id in potential_user_dirs:
            sessions.extend(_collect_sessions_in_dir(data_root / check_user_id, check_user_id))

        sessions = _finalize_session_rows(sessions)

        logger.info(f"Found {len(sessions)} sessions for user {user_id}")

    except Exception as exc:
        logger.error(f"Failed to list sessions: {exc}", exc_info=True)

    return sessions


def _get_all_sessions() -> List[Dict[str, Any]]:
    """Load sessions across ALL user directories under the data root.

    RC-2 (2026-07-07): own_ops/local ONLY — the single owner of this instance
    owns every session regardless of which surface/identity path tagged it
    (CLI sessions are user_id="local", telegram principals "u_<hash>", goal/
    cron runs the owner principal). Callers MUST gate on _catalog_scope();
    multitenant keeps strict per-tenant listing via _get_user_sessions.
    """
    sessions: List[Dict[str, Any]] = []
    try:
        data_root = pm().data_root
        logger.info(f"Loading ALL sessions from data_root: {data_root}")
        for user_path in data_root.iterdir():
            if not user_path.is_dir():
                continue
            sessions.extend(_collect_sessions_in_dir(user_path, user_path.name))
        sessions = _finalize_session_rows(sessions)
        logger.info(f"Found {len(sessions)} sessions across all users")
    except Exception as exc:
        logger.error(f"Failed to list all sessions: {exc}", exc_info=True)
    return sessions


def _catalog_scope(request: Request) -> tuple[str, Optional[str]]:
    """Who may list WHAT in the session catalog: ('all'|'user'|'none', user_id).

    Mirrors _check_session_ownership's posture logic exactly:
      - local: the loopback operator IS the owner → 'all'.
      - own_ops: the authenticated owner-login identity
        (webgate.local_owner_id()) → 'all'; any other identity or no auth →
        'none' (a non-owner in own_ops has no sessions of their own).
      - multitenant: authenticated → 'user' (strict per-tenant, unchanged);
        unauthenticated → 'none'.
    """
    if not webgate.requires_owner_login():
        return ("all", webgate.local_owner_id())

    from utils.auth_utils import is_authenticated
    if not is_authenticated(request):
        return ("none", None)
    current_user_id = getattr(request.state, 'user_id', None)

    if webgate.is_own_ops():
        if current_user_id and current_user_id == webgate.local_owner_id():
            return ("all", current_user_id)
        return ("none", current_user_id)

    return ("user", current_user_id) if current_user_id else ("none", None)


def _annotate_runtime(sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """WS-4 minimum (2026-07-07): say WHERE an active-looking session lives.

    Uses the P6 routing seam (TaskAgent.route_session → SessionRoute): with
    SESSION_REGISTRY_BACKEND=sqlite the registry mirrors liveness across
    processes, so the console can distinguish
      - 'agent': live in ANOTHER process (the agent service) — watch via feed,
        steering needs that process (honest 409 on send);
      - 'here':  resident in THIS process (console-created / resumed here);
      - 'idle':  no live orchestrator anywhere (resumable from disk).
    Only rows whose on-disk status looks active are queried (one registry
    lookup each); with the default in-process registry this degrades to
    here/idle, never lies. Fail-open: no agent → no annotation.
    """
    agent = _in_process_task_agent()
    route_fn = getattr(agent, "route_session", None) if agent else None
    if route_fn is None:
        return sessions
    for row in sessions:
        if row.get('status') not in ("running", "created", "resumed"):
            continue
        try:
            route = route_fn(row['id'])
        except Exception:
            continue
        if route is None or getattr(route, "is_missing", False):
            row['runtime'] = "idle"
        elif getattr(route, "is_remote", False):
            row['runtime'] = "agent"
            row['owner_pid'] = route.owner_pid
        else:
            row['runtime'] = "here"
    return sessions


def _sessions_for_request(request: Request) -> List[Dict[str, Any]]:
    """Catalog rows for this request, per _catalog_scope."""
    scope, user_id = _catalog_scope(request)
    if scope == "all":
        return _annotate_runtime(_get_all_sessions())
    if scope == "user":
        return _annotate_runtime(_get_user_sessions(user_id=user_id))
    return []


@_multitenant_get("/signin", response_class=HTMLResponse)
async def signin_page(request: Request) -> Response:
    """Show wallet sign in page."""
    from utils.auth_utils import is_authenticated
    return _templates.TemplateResponse("signin.html", {
        "request": request,
        "is_authenticated": is_authenticated(request),
        "is_admin": getattr(request.state, 'is_admin', False)
    })


@_multitenant_get("/logout", response_class=HTMLResponse)
async def logout(request: Request) -> Response:
    """Logout user and redirect to signin."""
    response = HTMLResponse(content="""
        <html>
            <head>
                <script>
                    localStorage.clear();
                    window.location.href = '/signin';
                </script>
            </head>
        </html>
    """)
    response.delete_cookie("auth_token")
    return response


# --- owner-login hardening: throttle + CSRF + return_to sanitizing ---------- #
# In-memory per-IP sliding window. Argon2 cost alone is not brute-force
# throttling; 5 attempts / 5 min per IP is generous for one owner.
_LOGIN_ATTEMPT_WINDOW_SEC = 300
_LOGIN_ATTEMPT_MAX = 5
_login_attempts: dict = {}


def _login_throttled(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_ATTEMPT_WINDOW_SEC]
    _login_attempts[ip] = attempts
    return len(attempts) >= _LOGIN_ATTEMPT_MAX


def _record_login_attempt(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())
    if len(_login_attempts) > 10000:  # bound memory under address churn
        _login_attempts.clear()


def _csrf_token_for(nonce: Optional[str]) -> Optional[str]:
    """Stateless double-submit token: HMAC(JWT_SECRET_KEY, nonce).

    None when no JWT secret is configured (local/dev without auth) — CSRF
    enforcement is skipped there, matching the no-auth posture.
    """
    secret = os.environ.get("JWT_SECRET_KEY")
    if not secret or not nonce:
        return None
    import hashlib
    import hmac as _hmac
    return _hmac.new(secret.encode(), f"owner-login:{nonce}".encode(), hashlib.sha256).hexdigest()


def _safe_return_to(raw) -> str:
    """Only same-origin relative paths — kills open redirects via return_to."""
    to = str(raw or "/")
    if not to.startswith("/") or to.startswith("//") or "\\" in to:
        return "/"
    return to


@_posture_get("/owner-login", postures=("own_ops", "multitenant"), response_class=HTMLResponse)
async def owner_login_page(request: Request) -> Response:
    """Owner username/password login page (Posture 1, own_ops).

    Registered for own_ops AND multitenant (NOT `_multitenant_get`, which
    would be multitenant-only) — own_ops posture is NOT multitenant, so this
    must stay reachable outside that gate. Wallet sign-in (`/signin`) stays
    available too, as an additional method in multitenant (design doc §1:
    owner-login is "optionally also selectable" there). Posture 0 (local)
    has no auth at all — no login surface needed or wanted, so it is NOT
    registered there (a request → 404).
    """
    import secrets as _secrets
    return_to = _safe_return_to(request.query_params.get("return_to", "/"))
    nonce = _secrets.token_hex(16)
    response = _templates.TemplateResponse("owner_login.html", {
        "request": request, "return_to": return_to, "error": None,
        "csrf_token": _csrf_token_for(nonce),
    })
    response.set_cookie(
        "csrf_nonce", nonce, max_age=600, httponly=True, samesite="lax",
        secure=(os.environ.get("ENVIRONMENT", "production") == "production"), path="/owner-login",
    )
    return response


@_posture_post("/owner-login", postures=("own_ops", "multitenant"))
async def owner_login_submit(request: Request) -> Response:
    """Verify owner credentials and, on success, issue the owner session cookie.

    Same error for a bad username or a bad password (no user-enumeration).
    Hardened: per-IP attempt throttle (429), stateless double-submit CSRF
    (403 on mismatch when a JWT secret is configured), sanitized return_to.
    """
    from webview.owner_auth import verify_owner_password, issue_owner_session_cookie

    client_ip = request.client.host if request.client else "unknown"
    if _login_throttled(client_ip):
        return _templates.TemplateResponse(
            "owner_login.html",
            {"request": request, "return_to": "/",
             "error": "Too many attempts. Try again in a few minutes.", "csrf_token": None},
            status_code=429,
        )
    _record_login_attempt(client_ip)

    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    return_to = _safe_return_to(form.get("return_to", "/"))

    expected_csrf = _csrf_token_for(request.cookies.get("csrf_nonce"))
    if os.environ.get("JWT_SECRET_KEY"):
        supplied = str(form.get("csrf_token", ""))
        if not expected_csrf or not hmac.compare_digest(supplied, expected_csrf):
            return _templates.TemplateResponse(
                "owner_login.html",
                {"request": request, "return_to": return_to,
                 "error": "Invalid or expired form. Please try again.", "csrf_token": None},
                status_code=403,
            )

    if not verify_owner_password(username, password):
        return _templates.TemplateResponse(
            "owner_login.html",
            {"request": request, "return_to": return_to,
             "error": "Invalid username or password.", "csrf_token": None},
            status_code=401,
        )

    response = RedirectResponse(url=return_to, status_code=303)
    issue_owner_session_cookie(response)
    return response


@_multitenant_get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request) -> Response:
    """Show user profile page with credits and deposit information."""
    from utils.auth_utils import is_authenticated
    return _templates.TemplateResponse("profile.html", {
        "request": request,
        "is_authenticated": is_authenticated(request),
        "is_admin": getattr(request.state, 'is_admin', False)
    })


@_fastapi.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> Response:
    """Show user settings page (MCP servers, preferences, API keys)."""
    from utils.auth_utils import is_authenticated
    return _templates.TemplateResponse("settings.html", {
        "request": request,
        "is_authenticated": is_authenticated(request),
        "is_admin": getattr(request.state, 'is_admin', False)
    })


# ============================================================================
# Admin Dashboard Routes
# ============================================================================

@_multitenant_get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request) -> Response:
    """Show admin dashboard - requires admin access."""
    from utils.auth_utils import is_authenticated

    # Check if user is admin
    is_admin = getattr(request.state, 'is_admin', False)
    if not is_admin:
        logger.warning(f"Non-admin attempted to access /admin: user_id={getattr(request.state, 'user_id', 'unknown')}")
        return HTMLResponse(content="""
            <html>
                <head>
                    <script>
                        alert('Admin access required');
                        window.location.href = '/';
                    </script>
                </head>
            </html>
        """, status_code=403)

    return _templates.TemplateResponse("admin/dashboard.html", {
        "request": request,
        "is_authenticated": is_authenticated(request),
        "is_admin": True
    })


@_multitenant_get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request) -> Response:
    """Show admin user management page."""
    from utils.auth_utils import is_authenticated

    is_admin = getattr(request.state, 'is_admin', False)
    if not is_admin:
        return RedirectResponse(url="/", status_code=303)

    return _templates.TemplateResponse("admin/users.html", {
        "request": request,
        "is_authenticated": is_authenticated(request),
        "is_admin": True
    })


@_multitenant_get("/admin/users/{user_id}", response_class=HTMLResponse)
async def admin_user_detail_page(request: Request, user_id: str) -> Response:
    """Show admin user detail page."""
    from utils.auth_utils import is_authenticated

    is_admin = getattr(request.state, 'is_admin', False)
    if not is_admin:
        return RedirectResponse(url="/", status_code=303)

    return _templates.TemplateResponse("admin/user_detail.html", {
        "request": request,
        "is_authenticated": is_authenticated(request),
        "is_admin": True,
        "target_user_id": user_id
    })


@_multitenant_get("/admin/activity", response_class=HTMLResponse)
async def admin_activity_page(request: Request) -> Response:
    """Show admin activity/audit log page."""
    from utils.auth_utils import is_authenticated

    is_admin = getattr(request.state, 'is_admin', False)
    if not is_admin:
        return RedirectResponse(url="/", status_code=303)

    return _templates.TemplateResponse("admin/activity.html", {
        "request": request,
        "is_authenticated": is_authenticated(request),
        "is_admin": True
    })


@_fastapi.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    """Posture-aware root.

    - local: full dashboard (unchanged).
    - own_ops/multitenant, unauthenticated: minimal public status page ONLY
      (safe-by-default invariant — no console/SaaS UI exposed by default).
    - own_ops/multitenant, authenticated (owner or, Posture 2, tenant session):
      full dashboard.

    Security:
        - `/` itself is intentionally NOT in `public_paths`/exempt from
          `auth_middleware`; enforcement here is the in-handler
          `is_authenticated(request)` check above, which branches by
          posture: local always gets the dashboard, own_ops/multitenant
          get the dashboard only once authenticated (owner or, Posture 2,
          a tenant session) and otherwise fall through to the public
          status page.
        - New sessions are always owned by the current user
    """
    from utils.auth_utils import is_authenticated

    if webgate.posture() != "local" and not is_authenticated(request):
        from core.instance import resolve_instance_id

        return _templates.TemplateResponse("status.html", {
            "request": request,
            "instance_id": resolve_instance_id(),
            "version": os.environ.get("WEBVIEW_VERSION", get_version()),
        })

    ws_url = os.environ.get("WEBVIEW_WS_URL", "")
    version = os.environ.get("WEBVIEW_VERSION", get_version())

    # For new sessions, the current user is always the owner
    is_owner = True
    user_is_authenticated = is_authenticated(request)

    return _templates.TemplateResponse(
        "session.html",
        {
            "request": request,
            "session_id": "new",  # Special value for empty state
            "ws_url": ws_url,
            "version": version,
            "is_owner": is_owner,
            "is_authenticated": user_is_authenticated,
            "is_admin": getattr(request.state, 'is_admin', False),
            "read_only": webgate.read_only(),
        },
    )


@_fastapi.get("/sessions", response_class=HTMLResponse)
async def sessions_list(request: Request) -> Response:
    """Show a list of sessions with metadata.

    Security:
        - own_ops/local: the instance owner sees ALL sessions (RC-2)
        - multitenant: only the authenticated tenant's own sessions
        - anyone else sees an empty list (no shared DEFAULT_USER_ID sessions)
    """
    from utils.auth_utils import is_authenticated

    # SECURITY: same scope rules as /api/sessions (_catalog_scope) — own_ops/
    # local owner sees ALL user dirs (RC-2), multitenant stays per-tenant,
    # everyone else gets an empty list.
    scope, user_id = _catalog_scope(request)
    sessions = _sessions_for_request(request)
    logger.info(f"Sessions list: scope={scope} user={user_id}, count={len(sessions)}")

    # Get WebSocket URL from environment variable
    ws_url = os.environ.get("WEBVIEW_WS_URL", "")

    # Get version from environment variable or use a default
    version = os.environ.get("WEBVIEW_VERSION", get_version())

    return _templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "sessions": sessions,
            "ws_url": ws_url,
            "version": version,
            "is_authenticated": is_authenticated(request),
            "is_admin": getattr(request.state, 'is_admin', False)
        },
    )


@_fastapi.get("/new", response_class=HTMLResponse)
async def new_session(request: Request) -> Response:
    """Alias for main chat page (backwards compatibility)."""
    return RedirectResponse(url="/", status_code=302)


@_fastapi.get("/session/{session_id}", response_class=HTMLResponse)
async def session_page(request: Request, session_id: str) -> Response:
    """Render the main session view.
    
    Security:
        - Viewing is allowed for anyone (no auth required)
        - Interaction (sending messages) requires ownership
        - Ownership is determined by matching user_id in session metadata
        
    Note:
        This endpoint is in public_paths, so auth middleware doesn't run.
        We manually check for authentication token to populate request.state.
    """
    from utils.auth_utils import is_authenticated, get_authenticated_user_id
    
    # Get WebSocket URL from environment variable
    ws_url = os.environ.get("WEBVIEW_WS_URL", "")

    # Get version from environment variable or use a default
    version = os.environ.get("WEBVIEW_VERSION", get_version())
    
    # Clean session ID
    clean_id = pm().clean_session_id(session_id)
    
    # Note: _manual_auth_check() is now called by auth middleware for all public paths
    # This populates request.state if a valid token is present
    
    # Check authentication and ownership using centralized helper
    is_owner, current_user_id, session_owner_id = _check_session_ownership(request, clean_id)
    user_is_authenticated = is_authenticated(request)
    
    # Log ownership status
    if user_is_authenticated and current_user_id:
        logger.info(f"Session {clean_id}: current_user={current_user_id}, owner={session_owner_id}, is_owner={is_owner}")
    else:
        logger.info(f"Session {clean_id}: viewing without authentication")

    return _templates.TemplateResponse(
        "session.html",
        {
            "request": request,
            "session_id": session_id,
            "ws_url": ws_url,
            "version": version,
            "is_owner": is_owner,
            "is_authenticated": user_is_authenticated,
            "is_admin": getattr(request.state, 'is_admin', False),
            "read_only": webgate.read_only(),
        },
    )


@_fastapi.get("/api/session/{session_id}/feed", response_class=JSONResponse)
async def api_feed(clean_id: str = Depends(get_clean_session_id)) -> Response:
    """DEPRECATED: Use /api/session/{id}/feed/events instead.
    
    This endpoint redirects to the newer, more flexible endpoint.
    Will be removed in a future version.
    """
    logger.warning(f"DEPRECATED endpoint /api/session/{clean_id}/feed called - use /feed/events instead")
    
    # Redirect to new endpoint with sensible defaults
    return RedirectResponse(
        url=f"/api/session/{clean_id}/feed/events?limit=500",
        status_code=307  # Temporary redirect, preserves method
    )


@_fastapi.get("/api/session/{session_id}/workspace/tree", response_class=JSONResponse)
async def api_workspace_tree(request: Request, clean_id: str = Depends(get_clean_session_id)) -> Response:
    """Return a very small file-tree of the session's workspace.
    
    Note: Uses session owner's user_id to allow public viewing of shared sessions.
    """
    logger.debug(f"API workspace tree request for cleaned ID: {clean_id}")

    try:
        # Use session owner's user_id (not requesting user's) to allow public viewing
        from utils.auth_utils import get_authenticated_user_id
        current_user_id = get_authenticated_user_id(request)
        session_owner_id = pm().get_session_user(clean_id)
        
        # Use session owner's ID for data access (allows public viewing)
        user_id = session_owner_id if session_owner_id else current_user_id

        ws_dir = pm().get_workspace_dir(clean_id, user_id=user_id)
        logger.debug(f"Getting workspace tree for {clean_id}, owner: {user_id}")
        
        if not ws_dir.exists():
            # Try to create workspace directory for just this session
            try:
                ws_dir.mkdir(parents=True, exist_ok=True)
                logger.info(f"Created missing workspace directory: {ws_dir}")
            except Exception as e:
                logger.error(f"Failed to create workspace directory: {e}")
                # Instead of an HTTP error, return empty tree with a message
                return JSONResponse({
                    "name": ws_dir.name,
                    "type": "dir",
                    "children": [],
                    "message": "Workspace directory does not exist or is not accessible"
                })

        def _walk(dir_path: Path) -> Dict[str, Any]:
            try:
                children = []
                # Skip if the directory doesn't exist or is not accessible
                if not dir_path.exists():
                    logger.warning(f"Directory does not exist: {dir_path}")
                    return children
                
                # Check if we can actually read the directory
                if not os.access(str(dir_path), os.R_OK):
                    logger.warning(f"Cannot read directory: {dir_path} (permission denied)")
                    return children
                    
                for child in sorted(dir_path.iterdir()):
                    # Skip hidden files/dirs at all directory levels (not just top level)
                    if child.name.startswith("."):
                        continue
                        
                    item = {"name": child.name, "type": "dir" if child.is_dir() else "file"}
                    if child.is_dir():
                        item["children"] = _walk(child)
                    children.append(item)
                return children
            except PermissionError:
                logger.warning(f"Permission denied when reading directory {dir_path}")
                return []
            except Exception as e:
                logger.error(f"Error walking directory {dir_path}: {e}")
                return []

        logger.debug(f"Building workspace tree for: {ws_dir}")
        children = _walk(ws_dir)
        
        # Log the result summary
        file_count = sum(1 for item in children if item["type"] == "file")
        dir_count = sum(1 for item in children if item["type"] == "dir")
        logger.info(f"Workspace tree for {clean_id}: {file_count} files, {dir_count} directories at top level")
        
        # Include empty message if no files
        if not children:
            return JSONResponse({
                "name": ws_dir.name,
                "type": "dir", 
                "children": [],
                "empty": True,
                "message": "No files found in workspace"
            })
        
        return JSONResponse({
            "name": ws_dir.name,
            "type": "dir", 
            "children": children
        })
    except Exception as e:
        logger.error(f"Error getting workspace tree for {clean_id}: {e}", exc_info=True)
        return JSONResponse({
            "name": "workspace",
            "type": "dir", 
            "children": [],
            "error": str(e),
            "message": "Error accessing workspace"
        }, status_code=500)


@_fastapi.get("/api/session/{session_id}/workspace/status", response_class=JSONResponse)
async def api_workspace_status(request: Request, clean_id: str = Depends(get_clean_session_id)) -> Response:
    """Diagnostic endpoint to check workspace directory status.

    Note: Uses session owner's user_id to allow public viewing of shared sessions.
    """
    try:
        # Use session owner's user_id (not requesting user's) to allow public viewing
        from utils.auth_utils import get_authenticated_user_id
        current_user_id = get_authenticated_user_id(request)
        session_owner_id = pm().get_session_user(clean_id)
        user_id = session_owner_id if session_owner_id else current_user_id

        # Get workspace directory
        ws_dir = pm().get_workspace_dir(clean_id, user_id=user_id)
        
        # Check if directory exists
        exists = ws_dir.exists()
        
        # Get directory stats if it exists
        stats = {}
        files = []
        if exists:
            try:
                # List top-level files/dirs
                top_items = list(ws_dir.iterdir())
                stats["item_count"] = len(top_items)
                stats["dirs"] = sum(1 for item in top_items if item.is_dir())
                stats["files"] = sum(1 for item in top_items if item.is_file())
                
                # List top 10 files for debugging
                for item in sorted(top_items)[:10]:
                    if item.is_file():
                        files.append({
                            "name": item.name,
                            "size": item.stat().st_size,
                            "modified": datetime.fromtimestamp(item.stat().st_mtime).isoformat()
                        })
                    elif item.is_dir():
                        # Count items in subdirectory
                        try:
                            subdir_items = list(item.iterdir())
                            files.append({
                                "name": f"{item.name}/",
                                "type": "directory",
                                "item_count": len(subdir_items)
                            })
                        except Exception as e:
                            files.append({
                                "name": f"{item.name}/",
                                "type": "directory",
                                "error": str(e)
                            })
            except Exception as e:
                stats["error"] = str(e)
        
        # Get full path
        full_path = str(ws_dir.resolve())
        
        # Check permissions
        permissions = {}
        try:
            permissions["readable"] = os.access(str(ws_dir), os.R_OK)
            permissions["writable"] = os.access(str(ws_dir), os.W_OK)
            permissions["executable"] = os.access(str(ws_dir), os.X_OK)
        except Exception as e:
            permissions["error"] = str(e)
            
        # Return detailed status
        return JSONResponse({
            "session_id": clean_id,
            "workspace_dir": str(ws_dir),
            "full_path": full_path,
            "exists": exists,
            "stats": stats,
            "permissions": permissions,
            "files": files
        })
    except Exception as e:
        logger.error(f"Error checking workspace status for {clean_id}: {e}", exc_info=True)
        return JSONResponse({
            "session_id": clean_id,
            "error": str(e)
        }, status_code=500)


@_fastapi.get("/api/session/{session_id}/workspace/file")
async def api_workspace_file(request: Request, path: str, clean_id: str = Depends(get_clean_session_id)) -> Response:  # noqa: WPS110 – param name dictated by API
    """Return the *text* content of a workspace file.
    
    Note: Uses session owner's user_id to allow public viewing of shared sessions.
    """
    logger.debug(f"API workspace file request for cleaned ID: {clean_id}, path: {path}")

    # Use session owner's user_id (not requesting user's) to allow public viewing
    from utils.auth_utils import get_authenticated_user_id
    current_user_id = get_authenticated_user_id(request)
    session_owner_id = pm().get_session_user(clean_id)
    
    # Use session owner's ID for data access (allows public viewing)
    user_id = session_owner_id if session_owner_id else current_user_id

    # FIXED: Enhanced security validation against directory traversal attacks
    import urllib.parse

    # First decode any URL encoding (including double encoding)
    decoded_path = path
    for _ in range(3):  # Decode up to 3 levels to catch double/triple encoding
        try:
            new_decoded = urllib.parse.unquote(decoded_path)
            if new_decoded == decoded_path:
                break  # No more decoding needed
            decoded_path = new_decoded
        except Exception:
            break

    # FIXED: Comprehensive path traversal prevention
    if any(dangerous in decoded_path.lower() for dangerous in [
        '..', './', '.\\.', '/.', '\\.',
        '%2e%2e', '%2f', '%5c',  # URL encoded variants
        'c:', 'd:', 'windows', 'system32',  # Windows system paths
        '/etc/', '/proc/', '/sys/', '/root/', '/home/'  # Unix system paths
    ]):
        logger.warning(f"Rejected dangerous path: {path} (decoded: {decoded_path})")
        raise HTTPException(403, "Forbidden: path contains dangerous sequences")

    # Additional check: path cannot start with / or \ (absolute paths)
    if decoded_path.startswith(('//', '\\\\', '/', '\\')):
        logger.warning(f"Rejected absolute path: {path} (decoded: {decoded_path})")
        raise HTTPException(403, "Forbidden: absolute paths not allowed")

    # Normalize the path to remove any remaining relative components
    import os.path
    normalized_path = os.path.normpath(decoded_path)

    # Final check: normalized path should not start with .. or contain ..
    if normalized_path.startswith('..') or '/..' in normalized_path or '\\..' in normalized_path:
        logger.warning(f"Rejected path after normalization: {normalized_path}")
        raise HTTPException(403, "Forbidden: path resolves outside workspace")

    workspace_dir = pm().get_workspace_dir(clean_id, user_id=user_id)
    file_path = workspace_dir / normalized_path
    
    # FIXED: Enhanced path resolution with security checks
    try:
        file_path = file_path.resolve()
        
        # Security: ensure the resolved path is still inside the workspace dir
        workspace_dir_resolved = workspace_dir.resolve()
        try:
            # Use relative_to to check if file_path is under workspace_dir_resolved
            file_path.relative_to(workspace_dir_resolved)
        except ValueError:
            logger.warning(f"Path resolves outside workspace: {file_path} not under {workspace_dir_resolved}")
            raise HTTPException(403, "Forbidden: path resolves outside workspace")
            
    except FileNotFoundError:
        raise HTTPException(404, "File not found") from None
    except OSError as e:
        logger.error(f"Error resolving file path: {e}")
        raise HTTPException(500, "Error processing file path") from None

    if not file_path.is_file():
        raise HTTPException(400, "Not a file")
        
    # FIXED: Enhanced file size and type checking for security
    try:
        file_size = file_path.stat().st_size
    except OSError:
        raise HTTPException(404, "Cannot access file")
        
    # Security: reject extremely large files to prevent DoS
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB limit
    if file_size > MAX_FILE_SIZE:
        logger.warning(f"Rejected oversized file: {file_path} ({file_size} bytes)")
        raise HTTPException(413, f"File too large (max {MAX_FILE_SIZE // 1024 // 1024}MB)")
    
    # Determine MIME type based on file extension with security filtering
    import mimetypes
    content_type, _ = mimetypes.guess_type(str(file_path))
    
    # FIXED: Security filter for content types
    ALLOWED_TEXT_TYPES = {
        'text/plain', 'text/html', 'text/css', 'text/javascript',
        'application/json', 'application/xml', 'text/xml',
        'text/markdown', 'text/csv', 'application/csv'
    }
    
    DANGEROUS_EXTENSIONS = {
        '.exe', '.bat', '.cmd', '.com', '.scr', '.pif',
        '.vbs', '.js', '.jar', '.ps1', '.sh'
    }
    
    file_extension = file_path.suffix.lower()
    
    # Block dangerous file types
    if file_extension in DANGEROUS_EXTENSIONS:
        logger.warning(f"Rejected dangerous file type: {file_path}")
        raise HTTPException(403, "Forbidden: dangerous file type")
    
    # Default to text/plain for unknown types with size limit for safety
    if not content_type:
        content_type = "text/plain"
    
    # For binary files, large files, or non-text types, return FileResponse with proper headers
    max_inline_size = 1024 * 1024  # 1MB limit for inline display
    
    if (content_type and not content_type.startswith("text/") and 
        content_type not in ALLOWED_TEXT_TYPES) or file_size > max_inline_size:
        
        # FIXED: Secure headers for file downloads
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(file_path.name)}",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Cache-Control": "no-store"
        }
        return FileResponse(file_path, media_type=content_type or "application/octet-stream", headers=headers)
    
    # For normal text files, return the content with security headers
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY", 
        "Cache-Control": "no-store, no-cache, must-revalidate"
    }
    
    return FileResponse(file_path, media_type=content_type, headers=headers)


@_fastapi.get("/api/session/{session_id}/workspace/serve/{path:path}")
async def api_workspace_serve(request: Request, path: str, clean_id: str = Depends(get_clean_session_id)) -> Response:
    """Serve workspace files for iframe embedding with relative path support.

    Unlike /workspace/file, this endpoint:
    - Allows iframe embedding (no X-Frame-Options DENY)
    - Supports relative paths for presentations/HTML apps
    - Returns files with proper content types
    """
    logger.debug(f"API workspace serve request for cleaned ID: {clean_id}, path: {path}")

    from utils.auth_utils import get_authenticated_user_id
    current_user_id = get_authenticated_user_id(request)
    session_owner_id = pm().get_session_user(clean_id)
    user_id = session_owner_id if session_owner_id else current_user_id

    import urllib.parse
    import os.path
    import mimetypes

    # Decode URL encoding
    decoded_path = path
    for _ in range(3):
        try:
            new_decoded = urllib.parse.unquote(decoded_path)
            if new_decoded == decoded_path:
                break
            decoded_path = new_decoded
        except Exception:
            break

    # Security: reject dangerous patterns
    if any(dangerous in decoded_path.lower() for dangerous in [
        '..', './', '.\\.', '/.', '\\.',
        '%2e%2e', '%2f', '%5c',
        'c:', 'd:', 'windows', 'system32',
        '/etc/', '/proc/', '/sys/', '/root/', '/home/'
    ]):
        logger.warning(f"Rejected dangerous path: {path}")
        raise HTTPException(403, "Forbidden: path contains dangerous sequences")

    if decoded_path.startswith(('//', '\\\\', '/', '\\')):
        raise HTTPException(403, "Forbidden: absolute paths not allowed")

    normalized_path = os.path.normpath(decoded_path)
    if normalized_path.startswith('..') or '/..' in normalized_path:
        raise HTTPException(403, "Forbidden: path resolves outside workspace")

    workspace_dir = pm().get_workspace_dir(clean_id, user_id=user_id)
    file_path = workspace_dir / normalized_path

    try:
        file_path = file_path.resolve()
        workspace_dir_resolved = workspace_dir.resolve()
        file_path.relative_to(workspace_dir_resolved)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "File not found")
    except OSError as e:
        logger.error(f"Error resolving file path: {e}")
        raise HTTPException(500, "Error processing file path")

    if not file_path.is_file():
        raise HTTPException(404, "File not found")

    # Get content type
    content_type, _ = mimetypes.guess_type(str(file_path))
    if not content_type:
        content_type = "application/octet-stream"

    # Security headers that allow iframe embedding
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store, no-cache, must-revalidate"
        # Note: No X-Frame-Options to allow iframe embedding
    }

    return FileResponse(file_path, media_type=content_type, headers=headers)


@_fastapi.get("/api/sessions", response_class=JSONResponse)
async def api_sessions(request: Request) -> Response:
    """Return a list of sessions with rich metadata for the authenticated user.

    SECURITY: Only returns sessions for the authenticated user from JWT token.
    Unauthenticated users get empty list (no shared DEFAULT_USER_ID sessions).
    """
    # SECURITY: scope comes from the posture + authenticated identity ONLY
    # (_catalog_scope): own_ops/local owner → ALL user dirs (RC-2); multitenant
    # stays strictly per-tenant; anyone else → empty list.
    scope, user_id = _catalog_scope(request)
    sessions = _sessions_for_request(request)
    logger.info(f"📊 Catalog scope={scope} user={user_id}: {len(sessions)} sessions")
    return JSONResponse({"sessions": sessions})


@_fastapi.get("/api/refresh", response_class=JSONResponse)
async def api_refresh() -> Response:
    """Force a refresh of all session data."""
    try:
        # Just return success - the UI will reload the page
        return JSONResponse({"status": "ok", "message": "Sessions refreshed"})
    except Exception as exc:
        logger.error("Failed to refresh sessions: %s", exc, exc_info=True)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@_fastapi.get("/api/repair/{session_id}", response_class=JSONResponse)
async def api_repair(clean_id: str = Depends(get_clean_session_id)) -> Response:
    """Repair a session's telemetry (dedup + token estimation + validation).

    Wired to the REAL ``webview.repair_sessions.repair_session_telemetry``
    (this endpoint used to return fake success without doing anything).
    Mutates feed/llm_usage files, so it is refused in read-only mode.
    """
    if webgate.read_only():
        return JSONResponse(
            {"status": "error", "message": "Console is read-only (WEBVIEW_READ_ONLY)"},
            status_code=403,
        )
    try:
        from webview.repair_sessions import repair_session_telemetry
        session_dir = pm().get_feed_dir(clean_id).parent
        if not session_dir.exists():
            return JSONResponse({"status": "error", "message": "Session not found"}, status_code=404)
        results = await asyncio.to_thread(repair_session_telemetry, session_dir)
        return JSONResponse({"status": "ok", "repair": results})
    except Exception as exc:
        logger.error("Failed to repair session %s: %s", clean_id, exc, exc_info=True)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@_fastapi.get("/api/session/{session_id}/screenshot", response_class=JSONResponse)
async def api_screenshot(request: Request, clean_id: str = Depends(get_clean_session_id)) -> Response:
    """Return the latest screenshot URL or data for a session.
    
    Note: Uses session owner's user_id to allow public viewing of shared sessions.
    """
    logger.debug(f"API screenshot request for cleaned ID: {clean_id}")

    try:
        # Use session owner's user_id (not requesting user's) to allow public viewing
        from utils.auth_utils import get_authenticated_user_id
        current_user_id = get_authenticated_user_id(request)
        session_owner_id = pm().get_session_user(clean_id)
        user_id = session_owner_id if session_owner_id else current_user_id

        session_dir = pm().get_feed_dir(clean_id, user_id=user_id).parent
        screenshot_dir = session_dir / "screenshots"
        
        # Check if the screenshots directory exists
        if not screenshot_dir.exists():
            # Try to create it, but no error if it already exists
            screenshot_dir.mkdir(exist_ok=True, parents=True)
            logger.info(f"Created screenshots directory for session {clean_id}")
        
        # Find the latest screenshot
        screenshots = []
        if screenshot_dir.exists():
            screenshots = sorted(
                [p for p in screenshot_dir.glob("*.png") if p.is_file()], 
                key=lambda p: p.stat().st_mtime, 
                reverse=True
            )
        
        if screenshots:
            # Get the most recent screenshot
            latest_screenshot = screenshots[0]
            # Return the path relative to static files or a direct file response
            screenshot_url = f"/api/session/{clean_id}/screenshot/file?ts={int(latest_screenshot.stat().st_mtime)}"
            
            # Try to get page URL from screenshot filename
            page_url = None
            try:
                # Extract URL from filename (handles both new and old formats)
                filename = latest_screenshot.stem
                
                # New format: screenshot_TIMESTAMP_URLENCODED.png
                # First check if there's at least one underscore
                if '_' in filename:
                    # Split at first underscore to separate timestamp and URL parts
                    parts = filename.split('_', 1)
                    
                    # If we have more parts, the second part might contain an encoded URL
                    if len(parts) > 1 and parts[1]:
                        # Check if there's another underscore - new format includes an underscore before the URL
                        if '_' in parts[1]:
                            # Get the URL part (everything after the second underscore)
                            url_part = parts[1].split('_', 1)[1]
                            if url_part:
                                import urllib.parse
                                # Try to decode URL - will be empty if parsing fails
                                page_url = url_part
                                # If URL wasn't encoded, try to reconstruct a usable URL
                                if not page_url.startswith('http'):
                                    # Check if it's a domain
                                    if '.' in page_url and 'www' in page_url or 'com' in page_url:
                                        page_url = f"https://{page_url}"
            except Exception as e:
                logger.debug(f"Error extracting URL from screenshot filename: {e}")
            
            # If we couldn't get URL from filename, try to get from recent step events
            if not page_url:
                try:
                    # Get the feed directory
                    feed_dir = pm().get_feed_dir(clean_id)
                    # Find the most recent step event that contains a URL
                    step_files = sorted(
                        feed_dir.glob("step_*.json"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True
                    )[:5]  # Check the 5 most recent step files
                    
                    for step_file in step_files:
                        try:
                            with step_file.open("r") as f:
                                step_data = json.load(f)
                                # Check different places where URL might be stored
                                if "data" in step_data:
                                    data = step_data["data"]
                                    # Check different possible URL fields
                                    for field in ["page_url", "current_url", "url"]:
                                        if field in data and data[field]:
                                            page_url = data[field]
                                            break
                                if page_url:
                                    break
                        except Exception:
                            continue
                except Exception as e:
                    logger.debug(f"Error checking step events for URL: {e}")
                
            return JSONResponse({
                "status": "ok", 
                "url": screenshot_url,
                "timestamp": latest_screenshot.stat().st_mtime,
                "filename": latest_screenshot.name,
                "page_url": page_url
            })
        else:
            # No screenshots found, return a placeholder
            logger.warning(f"No screenshots found for session {clean_id} in {screenshot_dir}")
            # Use an existing asset that we know exists to avoid 404s
            return JSONResponse({
                "status": "ok",
                "url": "/static/img/favicon.ico",
                "is_placeholder": True,
                "message": "No screenshots available"
            })
    except Exception as exc:
        logger.error("Failed to get screenshot for %s: %s", clean_id, exc, exc_info=True)
        return JSONResponse({
            "status": "error", 
            "message": str(exc),
            "url": "/static/img/favicon.ico",
            "is_placeholder": True
        }, status_code=500)


@_fastapi.get("/api/session/{session_id}/screenshot/file")
async def api_screenshot_file(request: Request, clean_id: str = Depends(get_clean_session_id), ts: int = None) -> Response:
    """Return the actual screenshot file.
    
    Note: Uses session owner's user_id to allow public viewing of shared sessions.
    """

    try:
        # Use session owner's user_id (not requesting user's) to allow public viewing
        from utils.auth_utils import get_authenticated_user_id
        current_user_id = get_authenticated_user_id(request)
        session_owner_id = pm().get_session_user(clean_id)
        user_id = session_owner_id if session_owner_id else current_user_id

        screenshot_dir = pm().get_feed_dir(clean_id, user_id=user_id).parent / "screenshots"
        
        if not screenshot_dir.exists():
            logger.warning(f"Screenshots directory not found for {clean_id}")
            raise HTTPException(404, "No screenshots directory found")
        
        screenshots = sorted(
            [p for p in screenshot_dir.glob("*.png") if p.is_file()], 
            key=lambda p: p.stat().st_mtime, 
            reverse=True
        )
        
        if not screenshots:
            logger.warning(f"No screenshots found for {clean_id}")
            raise HTTPException(404, "No screenshots found")
        
        # Include cache busting header to prevent browser caching
        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache"
        }
        
        return FileResponse(
            screenshots[0], 
            media_type="image/png",
            headers=headers
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to serve screenshot for %s: %s", clean_id, exc, exc_info=True)
        raise HTTPException(500, f"Error serving screenshot: {str(exc)}")


@_fastapi.get("/api/session/{session_id}/agents", response_class=JSONResponse)
async def api_agents(clean_id: str = Depends(get_clean_session_id)) -> Response:
    """Return the agents information for a session."""
    logger.debug(f"API agents request for cleaned ID: {clean_id}")
    
    try:
        # Look for an agents.json file in the session directory
        feed_dir = pm().get_feed_dir(clean_id)
        session_dir = feed_dir.parent
        agents_file = session_dir / "agents.json"
        
        # If agents.json exists, read it directly
        if agents_file.exists():
            try:
                with agents_file.open("r") as f:
                    cached_data = f.read()
                    if cached_data and len(cached_data) >= 10:
                        cached_agents = json.loads(cached_data)
                        
                        if cached_agents and isinstance(cached_agents, list) and len(cached_agents) > 0:
                            # Normalize the cached data
                            normalized_agents = []
                            for agent in cached_agents:
                                normalized_agent = {
                                    'id': agent.get('id', agent.get('agent_id', 'Unknown')),
                                    'name': agent.get('name', agent.get('agent_name', 'Unknown')),
                                    'type': agent.get('type', agent.get('agent_type', 'Unknown')),
                                    'model': agent.get('model', agent.get('model_name', ''))
                                }
                                normalized_agents.append(normalized_agent)
                                
                            logger.debug(f"Using cached agents data with {len(normalized_agents)} agents")
                            
                            return JSONResponse(
                                {"status": "ok", "agents": normalized_agents},
                                headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
                            )
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in agents file for session {clean_id}")
            except Exception as e:
                logger.warning(f"Error reading agents file: {e}")
        
        # If no agents.json or it's invalid, extract from feed files (READ-ONLY)
        agents = []
        agent_ids = set()
        agent_models = {}
        execution_sequence = []
        
        logger.debug(f"Extracting agents data from feed for session {clean_id}")
        
        if feed_dir.exists():
            # Check for multi_agent_relationship entries - they have the most complete agent info
            relationship_files = sorted(feed_dir.glob("*multi_agent_relationship*.json"), reverse=True)
            
            # Limit to latest 5 relationship files for efficiency
            for file in relationship_files[:5]:
                try:
                    with file.open("r") as f:
                        entry = json.load(f)
                        if entry.get("type") in ["multi_agent_relationship", "multi_agent_relationship_detailed"] and "data" in entry:
                            data = entry["data"]
                            # Look for agent_models first - this is the most reliable source
                            if "agent_models" in data and isinstance(data["agent_models"], dict):
                                for agent_id, model in data["agent_models"].items():
                                    if model:
                                        agent_models[agent_id] = model
                                        logger.debug(f"Found model info from relationship: {agent_id} -> {model}")
                            
                            # Store execution sequence if available
                            if "execution_sequence" in data and isinstance(data["execution_sequence"], list) and data["execution_sequence"]:
                                execution_sequence = data["execution_sequence"]
                                logger.debug(f"Found execution sequence with {len(execution_sequence)} agents")
                            elif "agent_ids" in data and isinstance(data["agent_ids"], list) and data["agent_ids"]:
                                execution_sequence = data["agent_ids"]
                                logger.debug(f"Using agent_ids as execution sequence with {len(execution_sequence)} agents")
                                                
                            # Process agent details
                            if "agent_details" in data and isinstance(data["agent_details"], list):
                                for agent_detail in data["agent_details"]:
                                    agent_id = agent_detail.get("id") or agent_detail.get("agent_id")
                                    if not agent_id:
                                        continue
                                        
                                    # Extract model info directly from agent_detail if available
                                    model_from_detail = agent_detail.get("model")
                                    if model_from_detail:
                                        agent_models[agent_id] = model_from_detail
                                        logger.debug(f"Found agent model in detail: {agent_id} -> {model_from_detail}")
                                    
                                    # Use the best available model info for this agent
                                    best_model = model_from_detail or agent_models.get(agent_id, '')
                                                        
                                    if agent_id not in agent_ids:
                                        agent = {
                                            'id': agent_id,
                                            'name': agent_detail.get("name") or agent_detail.get("agent_name") or agent_id,
                                            'type': agent_detail.get("type") or agent_detail.get("agent_type") or "Unknown",
                                            'model': best_model
                                        }
                                        agents.append(agent)
                                        agent_ids.add(agent_id)
                                        logger.debug(f"Added agent from relationship detail: {agent_id} with model {best_model}")
                                    # Update model for existing agents
                                    else:
                                        for agent in agents:
                                            if agent.get("id") == agent_id:
                                                if not agent.get("model") and best_model:
                                                    agent["model"] = best_model
                                                    logger.debug(f"Updated agent model: {agent_id} -> {best_model}")
                                                break
                except Exception as e:
                    logger.debug(f"Error processing relationship file {file}: {e}")
                    continue
            
            # Look for agent_registration entries to find more agents
            registration_files = sorted(feed_dir.glob("agent_registration_*.json"), reverse=True)
            
            # Limit to 20 most recent registration files
            for file in registration_files[:20]:
                try:
                    with file.open("r") as f:
                        entry = json.load(f)
                        if entry.get("type") == "agent_registration" and "data" in entry:
                            agent_data = entry["data"]
                            agent_id = agent_data.get("id") or agent_data.get("agent_id")
                            
                            if not agent_id:
                                # Try to extract agent_id from filename if not in data
                                filename = file.name
                                if "_" in filename:
                                    parts = filename.split("_")
                                    if len(parts) >= 3:  # agent_registration_AGENT_ID_TIMESTAMP.json
                                        possible_id = "_".join(parts[2:-1])
                                        if possible_id:
                                            agent_id = possible_id
                                            logger.debug(f"Extracted agent_id from filename: {agent_id}")
                            
                            if not agent_id:
                                continue
                            
                            # Extract model information from registration
                            model = None
                            if "model_name" in agent_data and agent_data["model_name"]:
                                model = agent_data["model_name"]
                            elif "model" in agent_data and agent_data["model"]:
                                model = agent_data["model"]
                            elif "llm_model" in agent_data and agent_data["llm_model"]:
                                model = agent_data["llm_model"]
                            
                            # Handle "Unknown" or "None" values
                            if model and (model == "Unknown" or model == "None"):
                                model = None
                            
                            # Store the model in our models dictionary for later use
                            if model:
                                agent_models[agent_id] = model
                                logger.debug(f"Stored model for agent {agent_id}: {model}")
                            
                            # Use the best available model
                            best_model = model or agent_models.get(agent_id, '')

                            if agent_id not in agent_ids:
                                agent = {
                                    'id': agent_id,
                                    'name': agent_data.get("name") or agent_data.get("agent_name") or agent_id,
                                    'type': agent_data.get("type") or agent_data.get("agent_type") or "Unknown",
                                    'model': best_model
                                }
                                agents.append(agent)
                                agent_ids.add(agent_id)
                                logger.debug(f"Added agent from registration: {agent_id} with model {best_model}")
                            # Update existing agents with better model info if needed
                            else:
                                for agent in agents:
                                    if agent.get("id") == agent_id:
                                        if not agent.get("model") or (best_model and len(best_model) > len(agent.get("model", ""))):
                                            agent["model"] = best_model
                                            logger.debug(f"Updated existing agent model: {agent_id} -> {best_model}")
                                        if agent.get("name") in [agent_id, "Unknown", "Agent"] and agent_data.get("name"):
                                            agent["name"] = agent_data.get("name") or agent_data.get("agent_name")
                                        if agent.get("type") == "Unknown" and agent_data.get("type"):
                                            agent["type"] = agent_data.get("type") or agent_data.get("agent_type")
                                        break
                except Exception as e:
                    logger.debug(f"Error processing agent registration file {file}: {e}")
                    continue
            
            # Look for LLM request data to capture model information
            llm_request_files = sorted(feed_dir.glob("llm_request_*.json"), reverse=True)
            
            # Limit to 30 most recent LLM requests
            for file in llm_request_files[:30]:
                try:
                    with file.open("r") as f:
                        entry = json.load(f)
                        if entry.get("type") == "llm_request" and "data" in entry:
                            data = entry["data"]
                            agent_id = data.get("agent_id")
                            
                            if not agent_id:
                                continue
                            
                            # Extract model information
                            model_name = None
                            if "model_name" in data and data["model_name"]:
                                model_name = data["model_name"]
                            elif "model" in data and data["model"]:
                                model_name = data["model"]
                            
                            # Update agent models if we found a model name
                            if agent_id and model_name:
                                agent_models[agent_id] = model_name
                                logger.debug(f"Updated agent model: {agent_id} -> {model_name}")
                                
                                # Update existing agents' model info if needed
                                for agent in agents:
                                    if agent.get("id") == agent_id and (not agent.get("model") or agent.get("model") == ''):
                                        agent["model"] = model_name
                                        logger.debug(f"Applied model to existing agent: {agent_id} -> {model_name}")
                except Exception as e:
                    logger.debug(f"Error processing LLM request file {file}: {e}")
                    continue
            
            # If we still don't have agent data, search in step files
            if not agents:
                step_files = sorted(feed_dir.glob("step_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
                for file in step_files:
                    try:
                        with file.open("r") as f:
                            entry = json.load(f)
                            if entry.get("type") == "step" and "data" in entry:
                                data = entry["data"]
                                agent_id_val = (
                                    data.get("agent_id")
                                    or data.get("id")
                                    or data.get("agent_name")
                                )
                                agent_name_val = data.get("agent_name") or data.get("name")
                                agent_type_val = data.get("agent_type") or data.get("type") or "Unknown"

                                if agent_id_val and agent_id_val not in agent_ids:
                                    agents.append({
                                        'id': agent_id_val,
                                        'name': agent_name_val or agent_id_val,
                                        'type': agent_type_val,
                                        'model': agent_models.get(agent_id_val, '')
                                    })
                                    agent_ids.add(agent_id_val)
                    except Exception as e:
                        logger.debug(f"Error processing step file {file}: {e}")
                        continue
        
        # Check metadata.json as a fallback for all agents
        try:
            metadata_file = session_dir / "metadata.json"
            if metadata_file.exists():
                with metadata_file.open("r") as f:
                    metadata_payload = json.load(f)
                    meta_agents = metadata_payload.get("agents", []) if isinstance(metadata_payload, dict) else []
                    for meta_agent in meta_agents:
                        meta_id = meta_agent.get("id") or meta_agent.get("agent_id")
                        if not meta_id:
                            continue
                        if meta_id not in agent_ids:
                            agents.append({
                                'id': meta_id,
                                'name': meta_agent.get('name') or meta_agent.get('agent_name') or meta_id,
                                'type': meta_agent.get('type') or meta_agent.get('agent_type') or 'Unknown',
                                'model': meta_agent.get('model') or meta_agent.get('model_name') or ''
                            })
                            agent_ids.add(meta_id)
                        else:
                            # Update existing agent entry with any missing details
                            for ag in agents:
                                if ag.get('id') == meta_id:
                                    if (not ag.get('name') or ag['name'] == 'Unknown') and (meta_agent.get('name') or meta_agent.get('agent_name')):
                                        ag['name'] = meta_agent.get('name') or meta_agent.get('agent_name')
                                    if (not ag.get('type') or ag['type'] == 'Unknown') and (meta_agent.get('type') or meta_agent.get('agent_type')):
                                        ag['type'] = meta_agent.get('type') or meta_agent.get('agent_type')
                                    if not ag.get('model') and (meta_agent.get('model') or meta_agent.get('model_name')):
                                        ag['model'] = meta_agent.get('model') or meta_agent.get('model_name')
                                    break
        except Exception as e:
            logger.debug(f"Error merging agents from metadata.json: {e}")
        
        # Ensure we're getting all unique agents with complete information
        unique_agents = {}
        for agent in agents:
            agent_id = agent.get('id')
            if agent_id:
                # If this agent ID already exists, merge information (favor non-empty values)
                if agent_id in unique_agents:
                    existing = unique_agents[agent_id]
                    # For each field, use the new value only if the existing one is empty
                    for field in ['name', 'type', 'model']:
                        if not existing.get(field) and agent.get(field):
                            existing[field] = agent.get(field)
                        # If the new value is longer or more specific, prefer it
                        elif existing.get(field) and agent.get(field) and len(agent.get(field)) > len(existing.get(field)):
                            # Only replace if it's a more informative value (longer and not just "Unknown")
                            field_value = agent.get(field)
                            if field_value and field_value.lower() != "unknown":
                                existing[field] = agent.get(field)
                else:
                    unique_agents[agent_id] = agent
        
        # Convert back to list
        agents = list(unique_agents.values())
        
        # Sort agents by execution order if possible, otherwise by ID
        try:
            if execution_sequence:
                # First sort by natural execution order using known sequence
                ordered_agents = []
                remaining_agents = []
                
                # First add agents in the execution sequence order
                for exec_id in execution_sequence:
                    for agent in agents:
                        if agent['id'] == exec_id:
                            ordered_agents.append(agent)
                            break
                
                # Then add any remaining agents not in the sequence
                for agent in agents:
                    if agent['id'] not in execution_sequence:
                        remaining_agents.append(agent)
                
                # Sort remaining agents by ID
                remaining_agents.sort(key=lambda a: a['id'])
                
                # Combine ordered and remaining agents
                agents = ordered_agents + remaining_agents
            else:
                # Fall back to sorting by agent ID
                agents.sort(key=lambda a: a['id'])
        except Exception as e:
            logger.debug(f"Error sorting agents: {e}")
            # If sorting fails, ensure we still return agents in some order
            agents.sort(key=lambda a: a.get('id', ''))
        
        # Return with Cache-Control header to prevent stale responses
        return JSONResponse(
            {"status": "ok", "agents": agents},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )
    except Exception as exc:
        logger.error("Failed to get agents for %s: %s", clean_id, exc, exc_info=True)
        return JSONResponse(
            {"status": "error", "message": str(exc)}, 
            status_code=500,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )


@_fastapi.get("/api/session/{session_id}/stats", response_class=JSONResponse)
async def api_stats(clean_id: str = Depends(get_clean_session_id)) -> Response:
    """Return statistics for a session."""
    try:
        feed_dir = pm().get_feed_dir(clean_id)

        stats = compute_session_stats(feed_dir)

        return JSONResponse(
            {"status": "ok", "data": stats},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )
    except Exception as exc:
        logger.error("Failed to get stats for %s: %s", clean_id, exc, exc_info=True)
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=500,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )


@_fastapi.get("/api/session/{session_id}/services", response_class=JSONResponse)
async def api_services(clean_id: str = Depends(get_clean_session_id)) -> Response:
    """Return the services information for a session."""
    logger.debug(f"API services request for cleaned ID: {clean_id}")
    
    try:
        # Look for a services.json file in the session directory
        feed_dir = pm().get_feed_dir(clean_id)
        services_file = feed_dir.parent / "services.json"
        
        # If services.json exists, read it directly
        if services_file.exists():
            try:
                with services_file.open("r") as f:
                    services_data = json.load(f)
                    
                return JSONResponse(
                    {"status": "ok", "services": services_data},
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
                )
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in services file for session {clean_id}")
            except Exception as e:
                logger.warning(f"Error reading services file: {e}")
        
        # If no services.json, extract service info from feed entries (READ-ONLY)
        services = {}
        
        if feed_dir.exists():
            # First, look for available_actions entries which have service grouping
            for file in sorted(feed_dir.glob("available_actions_*.json"), reverse=True):
                try:
                    with file.open("r") as f:
                        entry = json.load(f)
                        if entry.get("type") == "available_actions" and "data" in entry:
                            data = entry["data"]
                            if "by_service" in data and isinstance(data["by_service"], dict):
                                service_info = []
                                for service_name, actions in data["by_service"].items():
                                    service_info.append({
                                        "name": service_name,
                                        "type": "controller",
                                        "actions": actions,
                                        "action_count": len(actions)
                                    })
                                
                                if service_info:
                                    return JSONResponse(
                                        {"status": "ok", "services": service_info},
                                        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
                                    )
                except Exception as e:
                    logger.debug(f"Error processing services file {file}: {e}")
                    continue
            
            # If we didn't find service data, look at service_actions entries
            for file in sorted(feed_dir.glob("service_actions_*.json")):
                try:
                    with file.open("r") as f:
                        entry = json.load(f)
                        if entry.get("type") == "service_actions" and "data" in entry:
                            data = entry["data"]
                            service_name = data.get("service_name", "Unknown")
                            if service_name not in services:
                                services[service_name] = {
                                    "name": service_name,
                                    "type": data.get("service_type", "Unknown"),
                                    "actions": data.get("available_actions", []),
                                    "action_count": data.get("action_count", 0)
                                }
                except Exception as e:
                    logger.debug(f"Error processing {file}: {e}")
                    continue
            
            # If still no service data, extract from step actions
            if not services:
                for file in sorted(feed_dir.glob("step_*.json")):
                    try:
                        with file.open("r") as f:
                            entry = json.load(f)
                            if entry.get("type") == "step" and "data" in entry:
                                data = entry["data"]
                                if "actions" in data and isinstance(data["actions"], list):
                                    for action in data["actions"]:
                                        if "service" in action:
                                            service_name = action["service"]
                                            if service_name not in services:
                                                services[service_name] = {
                                                    "name": service_name,
                                                    "count": 0,
                                                    "actions": set()
                                                }
                                            services[service_name]["count"] += 1
                                            if "name" in action:
                                                services[service_name]["actions"].add(action["name"])
                    except Exception as e:
                        logger.debug(f"Error processing {file}: {e}")
                        continue
                
                # Convert sets to lists for JSON serialization
                for service in services.values():
                    if "actions" in service and isinstance(service["actions"], set):
                        service["actions"] = list(service["actions"])
        
        return JSONResponse(
            {"status": "ok", "services": list(services.values())},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )
    except Exception as exc:
        logger.error("Failed to get services for %s: %s", clean_id, exc, exc_info=True)
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=500,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )


@_fastapi.get("/api/session/{session_id}/task", response_class=JSONResponse)
async def api_task(clean_id: str = Depends(get_clean_session_id)) -> Response:
    """Return the current task for a session.
    
    This function prioritizes returning the initial user-inputted task,
    not subsequent task updates or derived goals.
    """
    logger.debug(f"API task request for cleaned ID: {clean_id}")
    
    try:
        # First look for a dedicated task.json file in the session directory
        session_dir = pm().get_feed_dir(clean_id).parent
        task_file = session_dir / "task.json"
        
        # If task.json exists, use it directly
        if task_file.exists():
            try:
                with task_file.open("r") as f:
                    task_data = json.load(f)
                    
                # Check if the task data is valid and contains the task
                if isinstance(task_data, dict) and "task" in task_data and task_data["task"]:
                    logger.info(f"Found task in task.json: '{task_data['task']}'")
                    # Include timestamp if available, or use file mtime
                    timestamp = task_data.get("timestamp") or task_file.stat().st_mtime
                    return JSONResponse(
                        {"status": "ok", "task": task_data["task"], "timestamp": timestamp},
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
                    )
            except Exception as e:
                logger.warning(f"Error reading task.json for {clean_id}: {e}")
        
        # Next check for metadata.json as it often contains the original task
        metadata_file = session_dir / "metadata.json"
        if metadata_file.exists():
            try:
                with metadata_file.open("r") as f:
                    metadata = json.load(f)
                    if "task" in metadata and metadata["task"]:
                        task = metadata["task"]
                        logger.info(f"Found initial task in metadata.json: '{task}'")
                        # Use metadata timestamp or file mtime
                        timestamp = metadata.get("created_at") or metadata.get("timestamp") or metadata_file.stat().st_mtime
                        return JSONResponse(
                            {"status": "ok", "task": task, "timestamp": timestamp},
                            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
                        )
            except Exception as e:
                logger.debug(f"Error reading metadata.json: {e}")
        
        # Look in the feed directory for session_start events which have the initial task
        feed_dir = pm().get_feed_dir(clean_id)
        if feed_dir.exists():
            # First check for session_start events (most reliable source for initial task)
            for file in sorted(feed_dir.glob("session_start_*.json"), reverse=True):
                try:
                    with file.open("r") as f:
                        entry = json.load(f)
                        if entry.get("type") == "session_start" and "data" in entry:
                            data = entry["data"]
                            if "task" in data and data["task"]:
                                task = data["task"]
                                # Use event timestamp or file mtime
                                timestamp = entry.get("timestamp") or file.stat().st_mtime
                                logger.info(f"Found initial task in session_start event: '{task}'")
                                
                                return JSONResponse(
                                    {"status": "ok", "task": task, "timestamp": timestamp},
                                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
                                )
                except Exception as e:
                    logger.debug(f"Error processing session start file {file}: {e}")
                    continue
            
            # Then check for task_update events (but only the first/earliest one which is likely initial)
            task_update_files = sorted(feed_dir.glob("task_update_*.json"))
            if task_update_files:
                # Only look at the first task_update file (chronologically) to get initial task
                try:
                    first_file = task_update_files[0]
                    with first_file.open("r") as f:
                        entry = json.load(f)
                        if entry.get("type") == "task_update" and "data" in entry:
                            data = entry["data"]
                            if "task" in data and data["task"]:
                                task = data["task"]
                                # Use event timestamp or file mtime
                                timestamp = entry.get("timestamp") or first_file.stat().st_mtime
                                logger.info(f"Found initial task in first task_update event: '{task}'")
                                
                                return JSONResponse(
                                    {"status": "ok", "task": task, "timestamp": timestamp},
                                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
                                )
                except Exception as e:
                    logger.debug(f"Error processing task update file: {e}")
        
        # No task was found anywhere
        logger.warning(f"No task information found for session {clean_id}")
        return JSONResponse(
            {"status": "not_found", "message": "No task information available"},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )
    
    except Exception as exc:
        logger.error("Failed to get task for %s: %s", clean_id, exc, exc_info=True)
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=500,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )


@_fastapi.get("/api/session/{session_id}/skills", response_class=JSONResponse)
async def api_skills(clean_id: str = Depends(get_clean_session_id)) -> Response:
    """Return the skills loaded for a session."""
    logger.debug(f"API skills request for cleaned ID: {clean_id}")
    
    try:
        # Look for a skills.json file in the session directory
        feed_dir = pm().get_feed_dir(clean_id)
        session_dir = feed_dir.parent
        skills_file = session_dir / "skills.json"
        
        # If skills.json exists, read it directly
        if skills_file.exists():
            try:
                with skills_file.open("r") as f:
                    skills_data = json.load(f)
                    
                return JSONResponse(
                    {"status": "ok", "skills": skills_data},
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
                )
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in skills file for session {clean_id}")
            except Exception as e:
                logger.warning(f"Error reading skills file: {e}")
        
        # If no skills.json, try to extract from session_start event
        if feed_dir.exists():
            for file in sorted(feed_dir.glob("session_start_*.json"), reverse=True):
                try:
                    with file.open("r") as f:
                        entry = json.load(f)
                        if entry.get("type") == "session_start" and "data" in entry:
                            data = entry["data"]
                            if "skills" in data and isinstance(data["skills"], list):
                                skills = data["skills"]
                                return JSONResponse(
                                    {"status": "ok", "skills": skills},
                                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
                                )
                except Exception as e:
                    logger.debug(f"Error processing session start file {file}: {e}")
                    continue
        
        # No skills data found - return empty list
        return JSONResponse(
            {"status": "ok", "skills": []},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )
    
    except Exception as exc:
        logger.error("Failed to get skills for %s: %s", clean_id, exc, exc_info=True)
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=500,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )


@_fastapi.get("/api/session/{session_id}/feed/events", response_class=JSONResponse)
async def api_feed_events(request: Request, session_id: str, event_type: Optional[str] = None, limit: int = FEED_DEFAULT_LIMIT, after_seq: Optional[int] = None) -> Response:
    """Return specific event types from the feed.

    Args:
        request: FastAPI request object (for user authentication)
        session_id: The session ID
        event_type: Optional filter by event type (step, planner, evaluation, etc.)
        limit: Maximum number of events to return (configurable, see FEED_DEFAULT_LIMIT/FEED_MAX_LIMIT)
        after_seq: Optional sequence number for delta sync - only return events with _seq > after_seq

    Note: Uses session owner's user_id to allow public viewing of shared sessions.

    Response includes metadata for delta sync:
        - last_seq: The highest sequence number in the returned events
        - total: Total number of events returned
    """
    # Validate limit parameter to prevent DoS
    if limit < 1 or limit > FEED_MAX_LIMIT:
        raise HTTPException(400, f"Limit must be between 1 and {FEED_MAX_LIMIT}")

    # Use session owner's user_id (not requesting user's) to allow public viewing
    from utils.auth_utils import get_authenticated_user_id
    current_user_id = get_authenticated_user_id(request)
    session_owner_id = pm().get_session_user(session_id)
    
    # Use session owner's ID for data access (allows public viewing)
    user_id = session_owner_id if session_owner_id else current_user_id

    # Clean the session ID to handle agent prefixes
    clean_id = pm().clean_session_id(session_id)
    logger.info(f"🔍 API feed events request for session {session_id} (cleaned: {clean_id}), user: {user_id}, type: {event_type}")

    try:
        feed_dir = pm().get_feed_dir(clean_id, user_id=user_id)
        logger.info(f"📁 Feed directory resolved to: {feed_dir}")
    except Exception as e:
        logger.error(f"Error getting feed directory: {e}")
        raise HTTPException(500, "Internal server error")

    if not feed_dir.exists():
        logger.warning(f"Feed directory not found: {feed_dir}")
        raise HTTPException(404, "Session not found")

    # Define valid event types
    valid_event_types = [
        'step', 'planner', 'evaluation', 'multi_agent_relationship',
        'agent_registration', 'session_start', 'task_update', 'llm_request',
        'service_actions', 'available_actions', 'status',
        'user_message', 'queue_status'  # Chat UI events
    ]

    # Validate event_type if provided
    if event_type and event_type not in valid_event_types:
        raise HTTPException(400, f"Invalid event_type. Must be one of: {', '.join(valid_event_types)}")

    # Get pattern for file search
    pattern = f"{event_type}_*.json" if event_type else "*.json"
    logger.info(f"🔎 Searching for pattern: {pattern}")

    # Find matching files, sorted by name (sequence-based filenames sort correctly)
    files = sorted(feed_dir.glob(pattern), key=lambda x: x.name)
    logger.info(f"📋 Found {len(files)} files matching pattern")

    # Process the files
    items = []
    last_seq = 0
    for file in files:
        try:
            with file.open("r") as fh:
                item = json.load(fh)

                # Check sequence-based filtering for delta sync
                item_seq = item.get('_seq')
                if after_seq is not None and item_seq is not None:
                    if item_seq <= after_seq:
                        continue  # Skip events we already have

                # Track highest sequence number
                if item_seq is not None and item_seq > last_seq:
                    last_seq = item_seq

                # Normalize to 'type' field (single source of truth)
                if 'event_type' in item and 'type' not in item:
                    item['type'] = item['event_type']

                # Add timestamp from filename if missing
                if 'timestamp' not in item:
                    ts_parts = file.stem.split('_')
                    if len(ts_parts) >= 2:
                        try:
                            item['timestamp'] = int(ts_parts[-1])
                        except ValueError:
                            item['timestamp'] = file.stat().st_mtime

                # Add source file for debugging
                item['_source_file'] = file.name

                # Enrich LLM cost if needed
                _enrich_llm_event_with_cost(item)

                items.append(item)

                # Check limit after processing (not before, to correctly handle after_seq filtering)
                if len(items) >= limit:
                    break
        except Exception as exc:
            logger.debug(f"Failed to read {file}: {exc}")

    # Return with metadata for delta sync
    return JSONResponse(
        {
            "events": items,
            "last_seq": last_seq,
            "total": len(items),
            "has_more": len(items) >= limit  # More events may exist if we hit the limit
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )


@_fastapi.post("/api/internal/emit")
async def internal_emit(request: Request) -> Response:
    """Internal endpoint for direct event emission from telemetry service.

    SECURITY: Only accepts requests from localhost (127.0.0.1).
    This endpoint bypasses the file watcher for immediate event delivery.

    Expected JSON body:
        {
            "session_id": "session_123",
            "event": { ... event data with _seq, _ts_ms, _id ... }
        }
    """
    # SECURITY: Localhost-only check
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        logger.warning(f"Internal emit rejected from non-localhost: {client_host}")
        raise HTTPException(403, "Forbidden: localhost only")

    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Internal emit: invalid JSON body: {e}")
        raise HTTPException(400, "Invalid JSON body")

    session_id = body.get("session_id")
    event = body.get("event")

    if not session_id or not event:
        raise HTTPException(400, "Missing session_id or event")

    # Clean the session ID for consistent room naming. NOTE: clients join the
    # room named by the BARE clean id (join_session → enter_room(sid, clean_id))
    # and the file watcher emits there too — a "session:" prefix here would be
    # a dead room nobody joins (the old bug that silenced this fast path).
    clean_id = pm().clean_session_id(session_id)
    room = clean_id

    # Enrich LLM cost if this is an llm_request event
    _enrich_llm_event_with_cost(event)

    # Emit directly to all clients in the session room
    await _sio.emit("feed_update", event, room=room)
    logger.debug(f"Internal emit: sent event to room {room}, _seq={event.get('_seq')}")

    return JSONResponse({"status": "ok", "room": room})


@_fastapi.get("/api/session/{session_id}/status", response_class=JSONResponse)
async def api_session_status(request: Request, session_id: str) -> Response:
    """Return the status of a session.

    Note: Uses session owner's user_id to allow public viewing of shared sessions.
    """
    # Clean the session ID to handle agent prefixes
    clean_id = pm().clean_session_id(session_id)
    logger.debug(f"API status request for session {session_id} (cleaned: {clean_id})")

    try:
        # Use session owner's user_id (not requesting user's) to allow public viewing
        from utils.auth_utils import get_authenticated_user_id
        current_user_id = get_authenticated_user_id(request)
        session_owner_id = pm().get_session_user(clean_id)
        user_id = session_owner_id if session_owner_id else current_user_id

        feed_dir = pm().get_feed_dir(clean_id, user_id=user_id)
        session_dir = feed_dir.parent
        
        if not feed_dir.exists():
            return JSONResponse(
                {"status": "unknown", "message": "Session not found"},
                status_code=404,
                headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
            )
        
        # Look for a status file
        status_file = session_dir / "status.json"
        status = "unknown"
        created_at = None
        
        # First check if we have created time from session directory
        try:
            # Try to get creation time from the session directory
            session_created_time = session_dir.stat().st_mtime
            created_at = session_created_time
        except Exception as e:
            logger.debug(f"Error getting session creation time: {e}")
        
        # Check for status.json
        if status_file.exists():
            try:
                with status_file.open("r") as f:
                    status_data = json.load(f)
                    status = status_data.get("status", "unknown")
                    # Use created_at from file if available
                    if "created_at" in status_data and not created_at:
                        created_at = status_data["created_at"]
            except Exception as e:
                logger.debug(f"Error reading status file: {e}")
        
        # If we don't have status yet, check for status in feed events
        if status == "unknown":
            # Look for status events in the feed
            status_files = sorted(feed_dir.glob("status_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if status_files:
                try:
                    with status_files[0].open("r") as f:
                        status_event = json.load(f)
                        if "data" in status_event and "status" in status_event["data"]:
                            status = status_event["data"]["status"]
                except Exception as e:
                    logger.debug(f"Error reading status event: {e}")
        
        # If we still don't have creation time, check for the oldest file
        if not created_at:
            try:
                all_files = list(feed_dir.glob("*.json"))
                if all_files:
                    oldest_file = min(all_files, key=lambda p: p.stat().st_mtime)
                    created_at = oldest_file.stat().st_mtime
            except Exception as e:
                logger.debug(f"Error getting oldest file: {e}")
                # Default to current time if we can't determine creation time
                created_at = time.time()
        
        # Determine status based on recent activity if we still don't have a clear status
        if status == "unknown":
            # Check if there are any recent files (in the last 5 minutes)
            now = time.time()
            recent_activity = False
            
            try:
                for file in feed_dir.glob("*.json"):
                    file_mtime = file.stat().st_mtime
                    if now - file_mtime < 300:  # 5 minutes
                        recent_activity = True
                        break
                
                # If there's recent activity, consider the session running
                if recent_activity:
                    status = "running"
                else:
                    # Check if there's any completion or error signals
                    for file in feed_dir.glob("*.json"):
                        try:
                            with file.open("r") as f:
                                entry = json.load(f)
                                if entry.get("type") == "status":
                                    data_status = entry.get("data", {}).get("status", "")
                                    if data_status in ["completed", "finished", "done"]:
                                        status = "completed"
                                        break
                                    elif data_status in ["failed", "error"]:
                                        status = "failed"
                                        break
                        except (json.JSONDecodeError, KeyError, TypeError):
                            pass  # Expected for non-JSON or malformed telemetry files
            except Exception as e:
                logger.debug(f"Error determining status based on activity: {e}")
                
        return JSONResponse(
            {
                "status": status,
                "created_at": created_at,
                "session_id": clean_id
            },
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )
    except Exception as exc:
        logger.error("Failed to get session status for %s: %s", clean_id, exc, exc_info=True)
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=500,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )


@_fastapi.get("/api/session/{session_id}/debug", response_class=JSONResponse)
async def api_session_debug(session_id: str) -> Response:
    """Debug endpoint to inspect session state and troubleshoot chat issues.

    Returns comprehensive information about:
    - Session directory structure
    - File existence (task.json, metadata.json, etc.)
    - Feed file counts by type
    - Sample feed files
    - Service connectivity status

    This endpoint is designed to help diagnose why the chat tab might be empty.
    """
    clean_id = pm().clean_session_id(session_id)
    logger.debug(f"API debug request for session {session_id} (cleaned: {clean_id})")

    try:
        feed_dir = pm().get_feed_dir(clean_id)
        session_dir = feed_dir.parent

        debug_info = {
            "session_id": session_id,
            "clean_id": clean_id,
            "paths": {
                "session_dir": str(session_dir),
                "feed_dir": str(feed_dir),
            },
            "existence": {
                "session_dir_exists": session_dir.exists(),
                "feed_dir_exists": feed_dir.exists(),
                "task_json_exists": (session_dir / "task.json").exists(),
                "metadata_json_exists": (session_dir / "metadata.json").exists(),
            },
            "feed_files": {},
            "task": None,
            "services": {
                "webview": "running",
                "main_api": None
            }
        }

        # Count feed files by type
        if feed_dir.exists():
            file_types = {}
            for file in feed_dir.glob("*.json"):
                file_type = file.name.split('_')[0]
                file_types[file_type] = file_types.get(file_type, 0) + 1

            debug_info["feed_files"] = {
                "total_count": sum(file_types.values()),
                "by_type": file_types,
                "files": [
                    {
                        "name": f.name,
                        "size": f.stat().st_size,
                        "modified": f.stat().st_mtime
                    }
                    for f in sorted(feed_dir.glob("*.json"), key=lambda x: x.stat().st_mtime)[:10]
                ]
            }

        # Try to load task
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                task_resp = await client.get(
                    f"http://localhost:8008/api/session/{session_id}/task",
                    timeout=2.0
                )
                if task_resp.status_code == 200:
                    debug_info["task"] = task_resp.json()
                else:
                    debug_info["task"] = {"error": f"HTTP {task_resp.status_code}"}
        except Exception as e:
            debug_info["task"] = {"error": str(e)}

        # Check main API
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                api_resp = await client.get("http://127.0.0.1:9000/health", timeout=2.0)
                debug_info["services"]["main_api"] = "running" if api_resp.status_code == 200 else "error"
        except Exception:
            debug_info["services"]["main_api"] = "not_reachable"

        return JSONResponse(
            debug_info,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )

    except Exception as e:
        logger.error(f"Debug endpoint error: {e}", exc_info=True)
        return JSONResponse(
            {"error": str(e), "session_id": session_id},
            status_code=500,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )


@_fastapi.get("/api/telemetry/health", response_class=JSONResponse)
async def api_telemetry_health() -> Response:
    """Return telemetry system health status."""
    try:
        from agents.task.telemetry.service import ProductTelemetry
        
        # Get the singleton telemetry instance
        telemetry = ProductTelemetry()
        health_stats = telemetry.get_health_stats()
        
        return JSONResponse({
            "status": "ok",
            "health": health_stats,
            "recommendations": _generate_health_recommendations(health_stats)
        }, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
        
    except Exception as exc:
        logger.error(f"Failed to get telemetry health: {exc}")
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=500,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )


def _generate_health_recommendations(stats: Dict[str, Any]) -> List[str]:
    """Generate health recommendations based on stats."""
    recommendations = []
    
    # Check success rates
    if stats.get('success_rate', 0) < 0.95:
        recommendations.append("Low telemetry capture success rate detected. Check LLM client error logs.")
    
    if stats.get('llm_capture_success_rate', 0) < 0.95 and stats.get('successful_llm_captures', 0) + stats.get('failed_llm_captures', 0) > 0:
        recommendations.append("Low LLM file capture success rate. Check file system permissions and disk space.")
    
    # Check rates
    if stats.get('duplicate_rate', 0) > 0.05:
        recommendations.append("High duplicate request rate detected. Check deduplication logic in stats service.")
    
    if stats.get('token_fallback_rate', 0) > 0.1:
        recommendations.append("Frequent token estimation fallbacks. Check LLM provider token reporting.")
    
    # Check for inactive system
    last_capture = stats.get('last_capture_time')
    if last_capture:
        import time
        time_since_last = time.time() - last_capture
        if time_since_last > 300:  # 5 minutes
            recommendations.append(f"No telemetry captures in {int(time_since_last/60)} minutes. System may be idle or experiencing issues.")
    
    # Check overall health status
    health_status = stats.get('status', 'unknown')
    if health_status == 'degraded':
        recommendations.append("System performance is degraded. Review logs for potential issues.")
    elif health_status == 'unhealthy':
        recommendations.append("System health is poor. Immediate attention required.")
    
    # Add proactive recommendations
    if stats.get('total_captures', 0) == 0:
        recommendations.append("No telemetry captures recorded. Verify LLM clients are properly configured.")
    
    uptime_hours = stats.get('uptime_seconds', 0) / 3600
    if uptime_hours > 24 and stats.get('total_captures', 0) < 100:
        recommendations.append("Low capture rate for system uptime. Check if agents are active.")
    
    return recommendations


async def _handle_stream_chunk(session_id: str, request: Request) -> Response:
    """Internal handler for streaming chunks - shared by both endpoints.

    SECURITY: This endpoint is only accessible from localhost (internal agent calls).
    External requests will be rejected with 403 Forbidden.
    """
    # SECURITY: Only allow requests from localhost (internal agent calls)
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(f"Rejected stream chunk from external host: {client_host}")
        return JSONResponse(
            {"success": False, "error": "Internal endpoint - localhost only"},
            status_code=403
        )

    clean_id = pm().clean_session_id(session_id)

    try:
        data = await request.json()
        chunk = data.get("chunk", "")
        agent_id = data.get("agent_id")
        step = data.get("step", 0)

        # Broadcast to all clients watching this session
        await _sio.emit("stream_chunk", {
            "session_id": clean_id,
            "agent_id": agent_id,
            "step": step,
            "chunk": chunk,
            "timestamp": time.time()
        }, room=clean_id)

        return JSONResponse({"success": True})

    except Exception as e:
        logger.error(f"Error receiving stream chunk: {e}")
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500
        )


@_fastapi.post("/api/session/{session_id}/stream", response_class=JSONResponse)
async def receive_stream_chunk_legacy(session_id: str, request: Request) -> Response:
    """Legacy endpoint for streaming chunks (backwards compatibility)."""
    return await _handle_stream_chunk(session_id, request)


@_fastapi.post("/api/webview/sessions/{session_id}/stream", response_class=JSONResponse)
async def receive_stream_chunk(session_id: str, request: Request) -> Response:
    """Receive streaming chunk from agent and broadcast to WebView clients.

    Agents call this endpoint to send LLM streaming output to connected browsers.
    """
    return await _handle_stream_chunk(session_id, request)


def _in_process_task_agent():
    """The TaskAgent living in THIS process (single-service webview deploys),
    or None when only the classic two-service (:9000) shape is available."""
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()
        agent = container.get_agent("task_agent")
        if not agent:
            agent = container.get_service("task_agent")
        return agent or None
    except Exception:
        return None


async def _send_message_in_process(request: Request, clean_id: str, agent,
                                   text: str, kind: str, metadata: dict,
                                   attached_files, session_owner_id,
                                   current_user_id) -> Response:
    """WS-3.1: deliver a session message via the in-process task router
    handler — the same code path the :9000 service would run (file
    verification, pre-queue race fix, resume states, guard_remote), with no
    network hop and no phantom service dependency.

    Authorization already happened in the wrapper via _check_session_ownership
    (the posture-aware authority: the own_ops/local owner owns EVERY session).
    The task handler's own _require_session_owner does a strict string match
    against the session's user_id, which would false-deny the owner on
    sessions tagged 'local'/'u_…' — so align the request identity to the
    session's owner for this internal, already-authorized call.
    """
    from api.task_http_api import send_user_message as _task_send
    from api.models import UserMessage as _UserMessage

    request.state.user_id = session_owner_id or current_user_id

    payload = _UserMessage(
        text=text,
        kind=kind or "comment",
        metadata=metadata or {},
        attached_files=attached_files,
    )
    try:
        await _task_send(clean_id, payload, request, agent)
    except HTTPException as e:
        if e.status_code == 409:
            # P6/Item 6 honest remote answer: the session is LIVE in another
            # process (the agent service) — watchable via the feed, not
            # steerable from here.
            return JSONResponse(
                {
                    "success": False,
                    "error": "Session is live in the agent process — watch it "
                             "via the feed; console steering requires the "
                             "session to be resident here.",
                    "detail": e.detail,
                },
                status_code=409,
                headers=getattr(e, "headers", None) or {},
            )
        if e.status_code == 404:
            return JSONResponse(
                {"success": False, "error": "Session not found or not active"},
                status_code=404,
            )
        detail = e.detail if isinstance(e.detail, str) else json.dumps(e.detail)
        return JSONResponse({"success": False, "error": detail},
                            status_code=e.status_code)

    logger.info(f"Message sent to session {clean_id} (in-process): '{text[:50]}...'")
    return JSONResponse({"success": True, "message": "Message sent"})


@_fastapi.post("/api/session/{session_id}/messages", response_class=JSONResponse)
async def send_message_to_session(session_id: str, request: Request) -> Response:
    """Send user message to running session.

    Delivers via the in-process TaskAgent when the task router is mounted in
    this process (single-service deploys, WS-3.1); otherwise proxies to the
    main :9000 API with retry logic (classic two-service shape).

    Security:
        - Requires authentication
        - Only session owner can send messages (_check_session_ownership,
          posture-aware: the own_ops/local owner owns every session)
    """
    clean_id = pm().clean_session_id(session_id)

    # Read-only console (monitoring deploys): no mutations, period.
    if webgate.read_only():
        return JSONResponse(
            {"success": False, "error": "Console is read-only (WEBVIEW_READ_ONLY)"},
            status_code=403,
        )

    # SECURITY: Check authentication and ownership using centralized helper
    is_owner, current_user_id, session_owner_id = _check_session_ownership(request, clean_id)

    # Check authentication
    if current_user_id is None:
        logger.warning(f"Unauthorized message attempt to session {clean_id} - not authenticated")
        return JSONResponse(
            {"success": False, "error": "Authentication required to send messages"},
            status_code=401
        )
    
    # Check ownership
    if not is_owner:
        logger.warning(f"Unauthorized message attempt to session {clean_id} - user {current_user_id} is not owner {session_owner_id}")
        return JSONResponse(
            {"success": False, "error": "Only session owner can send messages"},
            status_code=403
        )

    try:
        data = await request.json()
        text = data.get("text", "")
        kind = data.get("kind", "comment")
        metadata = data.get("metadata", {})
        attached_files = data.get("attached_files")  # NEW: Forward attached files for vision

        # WS-3.1 (2026-07-07): single-service deploys (prod own_ops) have NO
        # :9000 api service — but the task router + TaskAgent live in THIS
        # process. Deliver in-process when available; the :9000 proxy below
        # stays as the fallback for the classic two-service shape.
        in_proc_agent = _in_process_task_agent() if TASK_ROUTER_MOUNTED else None
        if in_proc_agent is not None:
            return await _send_message_in_process(
                request, clean_id, in_proc_agent, text, kind, metadata,
                attached_files, session_owner_id, current_user_id)

        import httpx

        # Call main API endpoint with retry logic
        api_url = "http://127.0.0.1:9000/api/task/sessions/{}/messages".format(clean_id)

        max_retries = 3
        retry_delay = 1.0  # seconds
        last_error = None

        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient() as client:
                    # NEW: Include attached_files in proxy request for vision support
                    payload = {"text": text, "kind": kind, "metadata": metadata}
                    if attached_files:
                        payload["attached_files"] = attached_files

                    response = await client.post(
                        api_url,
                        json=payload,
                        timeout=10.0
                    )

                    if response.status_code == 429:
                        # Rate limited - don't retry
                        logger.warning(f"Rate limit exceeded for session {clean_id}")
                        return JSONResponse(
                            {"success": False, "error": "Rate limit exceeded", "retry_after": 60},
                            status_code=429,
                            headers={"Retry-After": "60"}
                        )
                    elif response.status_code == 404:
                        # Session not found - log and return error
                        logger.error(f"Session {clean_id} not found in main API (404)")
                        return JSONResponse(
                            {"success": False, "error": "Session not found or not active"},
                            status_code=404
                        )
                    elif response.status_code >= 500:
                        # Server error - retry
                        logger.warning(f"Main API error {response.status_code} for session {clean_id}, attempt {attempt+1}/{max_retries}")
                        last_error = f"Server error: {response.status_code}"
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay * (attempt + 1))
                            continue
                    elif response.status_code >= 400:
                        # Client error - don't retry
                        logger.error(f"Client error {response.status_code} for session {clean_id}: {response.text}")
                        return JSONResponse(
                            {"success": False, "error": response.text or "Request failed"},
                            status_code=response.status_code
                        )

                    # Success
                    logger.info(f"Message sent to session {clean_id}: '{text[:50]}...'")
                    return JSONResponse({"success": True, "message": "Message sent"})

            except httpx.ConnectError as e:
                # Connection error - retry
                logger.error(f"Cannot connect to main API for session {clean_id}, attempt {attempt+1}/{max_retries}: {e}")
                last_error = f"Connection error: {str(e)}"
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    continue
                else:
                    # Last attempt failed
                    return JSONResponse(
                        {"success": False, "error": "Main API service unavailable. Please check if polyrob-api.service is running."},
                        status_code=503
                    )
            except httpx.TimeoutException as e:
                logger.error(f"Timeout sending message to session {clean_id}, attempt {attempt+1}/{max_retries}")
                last_error = "Request timeout"
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    continue

        # All retries exhausted
        logger.error(f"All {max_retries} retries exhausted for session {clean_id}. Last error: {last_error}")
        return JSONResponse(
            {"success": False, "error": last_error or "Request failed after retries"},
            status_code=503
        )

    except Exception as e:
        logger.error(f"Unexpected error sending message to session {clean_id}: {e}", exc_info=True)
        return JSONResponse(
            {"success": False, "error": f"Internal error: {str(e)}"},
            status_code=500
        )


@_fastapi.get("/api/session/{session_id}/queue-status", response_class=JSONResponse)
async def get_queue_status(session_id: str, request: Request) -> Response:
    """Get message queue status for a session.

    In-process TaskAgent when available (single-service deploys, WS-3.1);
    else proxies to the main :9000 API with retry logic (classic shape).
    """
    clean_id = pm().clean_session_id(session_id)

    # In-process path — same read-only data the :9000 handler would return.
    in_proc_agent = _in_process_task_agent() if TASK_ROUTER_MOUNTED else None
    if in_proc_agent is not None:
        from api.task_http_api import get_queue_status as _task_queue_status
        try:
            # The task handler gates on the session's OWN user_id; this wrapper
            # (like the proxy it replaces) exposes only queue depth/status, so
            # align the identity to the session owner for the internal call.
            owner = pm().get_session_user(clean_id)
            if owner:
                request.state.user_id = owner
            data = await _task_queue_status(clean_id, request, in_proc_agent)
            return JSONResponse({
                "queued_messages": data.get("queued_messages", 0),
                "agent_status": data.get("agent_status", "unknown"),
                "streaming_callbacks": data.get("streaming_callbacks", 0),
                "callback_failures": data.get("callback_failures", 0)
            })
        except HTTPException as e:
            if e.status_code == 404:
                return JSONResponse(
                    {"queued_messages": 0, "agent_status": "not_active"},
                    status_code=404)
            if e.status_code == 409:
                # Live in the agent process — honest, not an error state.
                return JSONResponse(
                    {"queued_messages": 0, "agent_status": "remote"})
            logger.warning(f"In-process queue-status failed for {clean_id}: {e.detail}")
            return JSONResponse({"queued_messages": 0, "agent_status": "error"})
        except Exception as e:
            logger.error(f"Unexpected in-process queue-status error for {clean_id}: {e}")
            return JSONResponse({"queued_messages": 0, "agent_status": "error"})

    try:
        import httpx

        # Call main API queue-status endpoint with retry logic
        api_url = f"http://127.0.0.1:9000/api/task/sessions/{clean_id}/queue-status"

        max_retries = 2  # Fewer retries for polling endpoint
        retry_delay = 0.5  # Shorter delay for polling

        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(api_url, timeout=5.0)

                    if response.status_code == 200:
                        data = response.json()
                        return JSONResponse({
                            "queued_messages": data.get("queued_messages", 0),
                            "agent_status": data.get("agent_status", "unknown"),
                            "streaming_callbacks": data.get("streaming_callbacks", 0),
                            "callback_failures": data.get("callback_failures", 0)
                        })
                    elif response.status_code == 404:
                        # Session not active - log once but return gracefully
                        if attempt == 0:
                            logger.info(f"Session {clean_id} not found or not active (404)")
                        return JSONResponse({
                            "queued_messages": 0,
                            "agent_status": "not_active"
                        }, status_code=404)
                    elif response.status_code >= 500:
                        # Server error - retry
                        logger.warning(f"Main API error {response.status_code} for queue-status, attempt {attempt+1}/{max_retries}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay)
                            continue
                    else:
                        # Other error from API
                        logger.warning(f"API queue-status returned {response.status_code} for session {clean_id}")
                        return JSONResponse({
                            "queued_messages": 0,
                            "agent_status": "error"
                        })

            except httpx.ConnectError as e:
                # Connection error - retry
                if attempt == 0:
                    logger.error(f"Cannot connect to main API for queue-status: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    return JSONResponse({
                        "queued_messages": 0,
                        "agent_status": "api_unavailable"
                    }, status_code=503)
            except httpx.TimeoutException:
                # Timeout - return unknown status
                logger.debug(f"Timeout getting queue status for session {clean_id}")
                return JSONResponse({
                    "queued_messages": 0,
                    "agent_status": "unknown"
                })

        # All retries exhausted (shouldn't reach here due to returns in loop)
        return JSONResponse({
            "queued_messages": 0,
            "agent_status": "error"
        })

    except Exception as e:
        logger.error(f"Unexpected error getting queue status for session {clean_id}: {e}", exc_info=True)
        return JSONResponse({
            "queued_messages": 0,
            "agent_status": "error"
        })


# NOTE: a second POST /api/webview/sessions/{id}/stream handler
# (api_session_stream, emitting "stream_update") used to live here. FastAPI
# routes first-match, so it was permanently shadowed by receive_stream_chunk
# above — dead code, removed. "stream_chunk" is the one streaming event.


# Socket.IO auth state: sid -> resolved user_id (None = anonymous/unauthenticated).
_socket_user: Dict[str, Optional[str]] = {}
# sid -> JWT tier claim ("admin" for owner-login tokens; None otherwise).
_socket_tier: Dict[str, Optional[str]] = {}
# sids currently in the global "activity" room (drives hub start/stop).
_activity_clients: set = set()


def _decode_socket_payload(token: Optional[str]) -> Dict:
    """Decode a client-supplied JWT into its full claim dict ({} on any error).
    Fail-open to anonymous — mirrors _manual_auth_check's tolerance."""
    if not token:
        return {}
    try:
        import jwt as pyjwt
        jwt_secret = os.environ.get("JWT_SECRET_KEY")
        if not jwt_secret:
            return {}
        return pyjwt.decode(token, jwt_secret, algorithms=["HS256"]) or {}
    except Exception as e:
        logger.debug(f"Socket.IO auth token decode failed: {e}")
        return {}


def _decode_socket_token(token: Optional[str]) -> Optional[str]:
    """Decode a client-supplied JWT from a Socket.IO connect() auth payload.
    Fail-open to None (anonymous) on any error."""
    return _decode_socket_payload(token).get("user_id")


def _socket_cookie_token(environ: Dict) -> Optional[str]:
    """Extract the ``auth_token`` cookie from a Socket.IO connect() environ.

    own_ops authenticates the owner via an httponly cookie minted by
    ``/owner-login`` (same cookie name the HTTP path reads in
    ``_manual_auth_check``/``auth_middleware``) — the browser's socket.io
    client never sees or forwards it as an ``auth={"token": ...}`` payload,
    so it has to be read off the raw ``HTTP_COOKIE`` header instead.
    """
    raw_cookie = (environ or {}).get("HTTP_COOKIE")
    if not raw_cookie:
        return None
    try:
        from http.cookies import SimpleCookie
        jar = SimpleCookie()
        jar.load(raw_cookie)
        morsel = jar.get("auth_token")
        return morsel.value if morsel else None
    except Exception as e:
        logger.debug(f"Socket.IO cookie parse failed: {e}")
        return None


@_sio.event
async def connect(sid: str, environ: Dict, auth: Dict | None = None) -> None:  # noqa: D401 – Socket.IO callback
    logger.debug("Client connected: %s", sid)
    # Posture "local": loopback operator IS the owner — no auth, no friction,
    # byte-identical to before. own_ops AND multitenant both require some
    # decoded identity (owner-login cookie or wallet/SIWE bearer token
    # respectively); short-circuiting only "local" here is what closes the
    # own_ops anonymous-socket gap (E4 follow-up) without touching Posture 0.
    if not webgate.requires_owner_login():
        _socket_user[sid] = webgate.local_owner_id()
        _socket_tier[sid] = None
        return
    token = (auth or {}).get("token") if isinstance(auth, dict) else None
    if not token:
        token = _socket_cookie_token(environ)
    payload = _decode_socket_payload(token)
    _socket_user[sid] = payload.get("user_id")
    _socket_tier[sid] = payload.get("tier")


async def _stop_hub_if_activity_empty() -> None:
    """Stop the activity hub's watcher/tails once nobody is watching."""
    if _activity_clients:
        return
    try:
        from webview.activity import get_hub
        await get_hub().aclose()
    except Exception as exc:
        logger.debug(f"activity hub stop failed: {exc}")


@_sio.event
async def disconnect(sid: str) -> None:  # noqa: D401 – Socket.IO callback
    """Handle client disconnect and clean up resources."""
    _socket_user.pop(sid, None)
    _socket_tier.pop(sid, None)
    if sid in _activity_clients:
        _activity_clients.discard(sid)
        await _stop_hub_if_activity_empty()
    # Figure out which session this sid belonged to
    sess_id = _client_session.get(sid)
    if sess_id is None:
        return

    # Remove client from room and tracking - use session_id directly
    await _sio.leave_room(sid, sess_id)
    _client_session.pop(sid, None)
    
    # Remove the client from this session
    _session_clients[sess_id] = _session_clients.get(sess_id, 1) - 1
    
    # Only cancel watcher if no more clients for this session
    if _session_clients.get(sess_id, 0) <= 0:
        # No more clients for this session - cancel the watcher
        task = _watch_tasks.pop(sess_id, None)
        if task and not task.done():
            logger.debug(f"Cancelling watcher task for session {sess_id}")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"Error cancelling watcher for {sess_id}: {exc}")
        
        # Clean up session tracking
        _session_clients.pop(sess_id, None)
    
    logger.debug("Client %s disconnected from %s (remaining=%s)", 
                sid, sess_id, _session_clients.get(sess_id, 0))


@_sio.event
async def join_session(sid, data):
    """Handle a client joining a session via Socket.io."""
    try:
        # Rate limiting check
        environ = _sio.get_environ(sid)
        client_ip = environ.get('REMOTE_ADDR', 'unknown')

        if not check_rate_limit(client_ip):
            logger.warning(f"Rate limit exceeded for IP {client_ip}")
            await _sio.emit("error", {
                "message": "Rate limit exceeded. Please wait before reconnecting."
            }, room=sid)
            await _sio.disconnect(sid)
            return

        session_id = data.get("session_id")
        logger.info("join_session: received session_id=%s from client %s", session_id, sid)
        if not session_id:
            await _sio.emit("error", {"message": "No session ID provided"}, room=sid)
            return

        # Clean the session ID to ensure consistency
        clean_id = pm().clean_session_id(session_id)
        logger.info("join_session: clean_id=%s for client %s", clean_id, sid)

        # E4 (A6 gap 1): tenant-gate the join BEFORE joining the room or streaming any feed.
        # requires_owner_login() covers BOTH own_ops (owner-login cookie) and
        # multitenant (wallet/SIWE JWT) — own_ops was left ungated (E4 follow-up).
        if webgate.requires_owner_login():
            current_user_id = _socket_user.get(sid)
            session_owner_id = pm().get_session_user(clean_id)
            is_owner = bool(current_user_id) and bool(session_owner_id) and current_user_id == session_owner_id
            if not is_owner:
                logger.warning(
                    "join_session denied: sid=%s user=%s session_owner=%s",
                    sid, current_user_id, session_owner_id,
                )
                await _sio.emit("error", {"message": "Not authorized to view this session"}, room=sid)
                return

        # Join socket.io room for this session - use clean_id as room name
        await _sio.enter_room(sid, clean_id)
        _client_session[sid] = clean_id
        _session_clients[clean_id] = _session_clients.get(clean_id, 0) + 1
        logger.info("Client %s joined session %s (cleaned: %s)", sid, session_id, clean_id)

        # Send initial feed data to the client
        feed_dir = pm().get_feed_dir(clean_id)
        logger.info("join_session: checking feed_dir=%s, exists=%s", feed_dir, feed_dir.exists())
        if feed_dir.exists():
            # Read all JSON files in feed dir chronologically
            json_files = sorted(feed_dir.glob("*.json"))
            logger.info("join_session: found %d JSON files in feed_dir", len(json_files))
            feed_entries = []

            # Read each file and parse the event
            for file_path in json_files:
                try:
                    with file_path.open("r") as f:
                        entry = json.load(f)
                        # Include only events that have valid format
                        if entry and isinstance(entry, dict) and "type" in entry:
                            # Enrich LLM request entries with cost estimates if missing
                            _enrich_llm_event_with_cost(entry)

                            feed_entries.append(entry)
                except Exception as exc:
                    logger.error("Failed to parse feed file %s: %s", file_path, exc)
                    continue

            # Send the feed entries to the client
            # RAM optimization: Chunk large feed data to prevent memory spikes
            if len(feed_entries) > 100:  # Increased threshold back to 100
                chunk_size = 50  # Increased chunk size back to 50
                for i in range(0, len(feed_entries), chunk_size):
                    chunk = feed_entries[i:i + chunk_size]
                    is_last_chunk = i + chunk_size >= len(feed_entries)

                    await _sio.emit("initial_feed_chunk", {
                        "chunk": chunk,
                        "chunk_index": i // chunk_size,
                        "total_chunks": (len(feed_entries) + chunk_size - 1) // chunk_size,
                        "is_last": is_last_chunk
                    }, room=sid)

                    # Removed artificial delay - was causing lag
                    
                logger.info("Sent %d feed entries in %d chunks to client %s",
                           len(feed_entries), (len(feed_entries) + chunk_size - 1) // chunk_size, sid)
            else:
                # Send small feeds normally
                logger.info("join_session: sending initial_feed with %d entries to %s", len(feed_entries), sid)
                await _sio.emit("initial_feed", json.dumps(feed_entries), room=sid)
                logger.info("Sent %d initial feed entries to client %s", len(feed_entries), sid)
        else:
            logger.warning("Feed directory %s does not exist", feed_dir)
            await _sio.emit("initial_feed", "[]", room=sid)
            
        # Ensure a watcher is running for this session - use clean_id for watcher key
        if clean_id not in _watch_tasks:
            _watch_tasks[clean_id] = asyncio.create_task(_feed_watcher(clean_id))
            logger.debug("Started feed watcher task for session %s", clean_id)

    except Exception as exc:
        logger.error("Error in join_session handler: %s", exc, exc_info=True)
        await _sio.emit("error", {"message": str(exc)}, room=sid)


@_sio.event
async def join_activity(sid, data=None):
    """Join the global activity room (the /activity terminal's live stream).

    The stream is inherently cross-tenant, so it is gated harder than
    join_session: local = open (loopback operator); own_ops/multitenant =
    the instance owner or an admin-tier JWT ONLY. On the first watcher the
    ActivityHub lazily starts its feed watcher + DB tails; it stops when the
    room empties (leave_activity/disconnect).
    """
    try:
        environ = _sio.get_environ(sid) or {}
        client_ip = environ.get("REMOTE_ADDR", "unknown")
        if not check_rate_limit(client_ip):
            logger.warning(f"join_activity rate limit exceeded for IP {client_ip}")
            await _sio.emit("error", {
                "message": "Rate limit exceeded. Please wait before reconnecting."
            }, room=sid)
            await _sio.disconnect(sid)
            return

        if not webgate.activity_enabled():
            await _sio.emit("error", {"message": "Activity stream disabled"}, room=sid)
            return

        if webgate.requires_owner_login():
            current_user_id = _socket_user.get(sid)
            tier = _socket_tier.get(sid)
            allowed = bool(current_user_id) and (
                current_user_id == webgate.local_owner_id() or tier == "admin"
            )
            if not allowed:
                logger.warning(
                    "join_activity denied: sid=%s user=%s tier=%s", sid, current_user_id, tier
                )
                await _sio.emit("error", {
                    "message": "Not authorized for the activity stream"
                }, room=sid)
                return

        from webview.activity import get_hub
        await _sio.enter_room(sid, "activity")
        _activity_clients.add(sid)
        hub = get_hub()
        hub.start(_sio)
        await _sio.emit("activity_snapshot", hub.recent(200), room=sid)
        logger.info("Client %s joined the activity stream (watchers=%d)",
                    sid, len(_activity_clients))
    except Exception as exc:
        logger.error(f"join_activity error: {exc}", exc_info=True)
        await _sio.emit("error", {"message": "Failed to join activity stream"}, room=sid)


@_sio.event
async def leave_activity(sid):
    """Leave the global activity room; stop the hub when it empties."""
    try:
        await _sio.leave_room(sid, "activity")
    except Exception:
        pass
    _activity_clients.discard(sid)
    await _stop_hub_if_activity_empty()


@_sio.event
async def leave(sid):
    """Handle a client disconnecting from Socket.io."""
    session_id = _client_session.get(sid)
    if session_id:
        await _sio.leave_room(sid, session_id)
        _client_session.pop(sid, None)
        
        # Decrement client count for this session
        _session_clients[session_id] = max(0, _session_clients.get(session_id, 1) - 1)
        
        # If no clients left, cancel the watcher task
        if _session_clients[session_id] == 0 and session_id in _watch_tasks:
            _watch_tasks[session_id].cancel()
            try:
                await _watch_tasks[session_id]
            except asyncio.CancelledError:
                pass
            del _watch_tasks[session_id]
            logger.debug("Cancelled feed watcher for session %s", session_id)
            
        logger.info("Client %s left session %s", sid, session_id)


async def _feed_watcher(session_id: str) -> None:
    """Watch for changes in a session's feed directory and notify clients.
    
    This function monitors the feed directory for new JSON files and parses them
    to send real-time updates to connected clients via websockets.
    
    Args:
        session_id: The session ID to watch
    """
    clean_id = pm().clean_session_id(session_id)
    feed_dir = pm().get_feed_dir(clean_id)
    logger.info("Starting feed watcher for session %s at %s", session_id, feed_dir)
    
    # Use session_id directly as the room name
    room = session_id

    if not feed_dir.exists():
        logger.warning("Feed directory %s does not exist, watcher will wait", feed_dir)
        # Create the directory if it doesn't exist
        feed_dir.mkdir(parents=True, exist_ok=True)

    # Track processed files to avoid duplicates - use bounded set for memory efficiency
    processed_files = set()
    max_processed_files = 1000  # Reduced from 5000 for better performance
    
    try:
        # Use native filesystem watching instead of polling for better performance
        async for changes in awatch(feed_dir, watch_filter=lambda change, path: path.endswith('.json')):
            try:
                for change_type, file_path_str in changes:
                    # Only process new or modified files
                    if change_type not in (Change.added, Change.modified):
                        continue

                    file_path = Path(file_path_str)

                    # Skip if already processed
                    if file_path.name in processed_files:
                        continue

                    try:
                        # Add to processed set right away to avoid race conditions
                        processed_files.add(file_path.name)

                        # Parse the file
                        with file_path.open("r") as f:
                            entry = json.load(f)

                        # Skip invalid entries
                        if not entry or not isinstance(entry, dict) or "type" not in entry:
                            logger.warning("Invalid feed entry in %s, missing 'type'", file_path)
                            continue

                        # Enrich LLM request entries with cost estimates if missing
                        _enrich_llm_event_with_cost(entry)

                        # Send the entry to all clients in the room (rate-limited, E5)
                        await _emit_feed_event(entry, room)
                        logger.debug("Sent feed update for file %s to room %s", file_path.name, room)

                    except Exception as exc:
                        logger.error("Error processing feed file %s: %s", file_path, exc)
                        continue

                # RAM optimization: Prevent processed files set from growing too large
                if len(processed_files) > max_processed_files:
                    # Keep only the most recent files based on filename (which includes timestamp)
                    sorted_files = sorted(processed_files)
                    keep_count = max_processed_files // 2  # Keep half when trimming
                    processed_files = set(sorted_files[-keep_count:])
                    logger.debug(f"Trimmed processed files set from {len(sorted_files)} to {len(processed_files)}")

            except asyncio.CancelledError:
                logger.info("Feed watcher for session %s was cancelled", session_id)
                break

            except Exception as exc:
                logger.error("Error in feed watcher for %s: %s", session_id, exc, exc_info=True)
                # Continue watching despite errors
                await asyncio.sleep(1)
                
    finally:
        logger.info("Feed watcher for session %s exiting", session_id)


async def _sweep_expired_nonces_once(siwe_auth) -> None:
    """Single sweep tick — factored out of the loop for testability. Fail-open:
    a sweep error must never take down the background task (E2)."""
    try:
        await siwe_auth.cleanup_expired_nonces()
    except Exception as exc:
        logger.warning(f"Nonce sweep failed: {exc}")


async def _nonce_sweep_loop(siwe_auth, interval_seconds: int = 300) -> None:
    while True:
        await _sweep_expired_nonces_once(siwe_auth)
        await asyncio.sleep(interval_seconds)


async def _startup_late_services():
    """Auth/task/wallet startup half — merged into the single startup_event.

    Historically this was a SECOND ``@_fastapi.on_event("startup")`` handler
    competing with the first (both ran, container init duplicated); it is now
    invoked explicitly at the end of ``startup_event`` in the same order
    FastAPI would have run it.
    """
    global _container, _wallet_generator

    # Initialize deposit wallet generator (lazy import to avoid circular dependency)
    from core.payment_config import resolve_master_seed
    master_seed = resolve_master_seed()
    if master_seed:
        try:
            from modules.payments.wallet_generator import DepositWalletGenerator
            _wallet_generator = DepositWalletGenerator(master_seed)
            logger.info("✅ Deposit wallet generator initialized")
        except Exception as e:
            logger.error(f"❌ Failed to initialize wallet generator: {e}", exc_info=True)
    else:
        logger.warning("⚠️ PAYMENT_MASTER_SEED not configured - deposit address generation disabled")

    if not AUTH_ROUTER_MOUNTED and not TASK_ROUTER_MOUNTED:
        logger.warning("⚠️ No routers mounted, skipping container initialization")
        return

    try:
        logger.info("🚀 Initializing dependency container...")
        from core.container import DependencyContainer

        # Try to load full config, but fallback to minimal config if needed
        try:
            from core.config import BotConfig
            config = BotConfig()
            logger.info("✅ Full bot config loaded")
        except Exception as config_error:
            logger.warning(f"⚠️ Could not load full bot config: {config_error}")
            logger.info("Using minimal config for webview-only services")
            # Create a minimal config object with just what we need
            from types import SimpleNamespace
            config = SimpleNamespace()
            config.session_ttl_seconds = 3600
            config.max_sessions_in_memory = 100
            config.session_cleanup_interval = 300

        # Initialize container singleton
        _container = DependencyContainer.get_instance(config)

        # Verify auth services if auth router is mounted
        if AUTH_ROUTER_MOUNTED:
            siwe_auth = _container.get_service('siwe_authenticator')
            identity_mapper = _container.get_service('identity_mapper')

            if siwe_auth and identity_mapper:
                logger.info("✅ Auth services initialized successfully")
                logger.info(f"   - SIWE Authenticator: {type(siwe_auth).__name__}")
                logger.info(f"   - Identity Mapper: {type(identity_mapper).__name__}")
            else:
                logger.warning("⚠️ Some auth services not available")
                logger.warning(f"   - SIWE Auth: {siwe_auth is not None}")
                logger.warning(f"   - Identity Mapper: {identity_mapper is not None}")

            # E2: periodic nonce-expiry sweep — SIWE (and auth_nonces) only
            # exists in the multitenant posture.
            if webgate.is_multitenant():
                try:
                    if siwe_auth:
                        asyncio.create_task(_nonce_sweep_loop(siwe_auth))
                        logger.info("✅ Nonce expiry sweep started (5min interval)")
                except Exception as e:
                    logger.warning(f"⚠️ Could not start nonce sweep: {e}")

        # Initialize task agent if task router is mounted AND config is available
        if TASK_ROUTER_MOUNTED:
            try:
                # Only initialize if we have a real BotConfig
                from core.config import BotConfig
                if isinstance(config, BotConfig):
                    logger.info("🚀 Initializing task agent...")
                    from agents.task_agent_lite import TaskAgent

                    # Create and register task agent
                    task_agent = TaskAgent(name="task_agent", config=config, container=_container)
                    await task_agent.initialize()
                    _container.register_agent("task_agent", task_agent)

                    logger.info("✅ Task agent initialized successfully")
                    logger.info(f"   - Task Agent: {type(task_agent).__name__}")
                    logger.info(f"   - Session Manager Available: {task_agent.session_manager is not None}")
                else:
                    logger.warning("⚠️ Task agent requires full bot config - skipping initialization")
                    logger.info("Task endpoints will not be available")

            except Exception as e:
                logger.error(f"❌ Failed to initialize task agent: {e}", exc_info=True)
                logger.warning("⚠️ Task session creation will not work!")

    except Exception as e:
        logger.error(f"❌ Failed to initialize container: {e}", exc_info=True)
        logger.warning("⚠️ Services will not work without container!")
        _container = None


@_fastapi.on_event("shutdown")
async def shutdown_event():
    """Clean up resources when the server is shutting down."""
    logger.info("Server shutting down, cancelling all watcher tasks...")
    try:
        from webview.activity import get_hub
        await get_hub().aclose()
    except Exception as exc:
        logger.debug(f"activity hub shutdown cleanup failed: {exc}")
    for session_id, task in _watch_tasks.items():
        if not task.done():
            logger.debug(f"Cancelling watcher task for session {session_id}")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"Error cleaning up watcher for {session_id}: {exc}")
    _watch_tasks.clear()
    _session_clients.clear()
    _client_session.clear()


@_fastapi.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    """Render a custom error page for HTTP errors."""
    status_code = exc.status_code
    detail = exc.detail
    
    # Get WebSocket URL from environment variable
    ws_url = os.environ.get("WEBVIEW_WS_URL", "")
    
    # Get version from environment variable or use a default
    version = os.environ.get("WEBVIEW_VERSION", get_version())
    
    return _templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "error": f"HTTP {status_code}",
            "message": detail,
            "troubleshooting": [
                "The session may not exist or has been deleted.",
                "Check the URL and try again."
            ],
            "ws_url": ws_url,
            "version": version
        },
        status_code=status_code
    )