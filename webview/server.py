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

from collections import OrderedDict
from pathlib import Path
import asyncio
import json
from core.security.session_tokens import decode_session_token
from webview.session_identity import require_session_identity

import logging
from typing import Any, Dict, List, Optional
import os
from datetime import datetime
import time
import hashlib
import hmac

import socketio
from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from watchfiles import awatch, Change

# Local utility that aggregates session statistics from feed files
from webview.stats_service import compute_session_stats

# Shared read-service: reconstruct the multi-agent roster from the session feed
from agents.task.telemetry.agent_graph import build_session_agents
from agents.task.telemetry.feed_reads import build_session_services, build_session_task, build_session_skills

# Webgate config object — single source of truth for single-user (default) vs
# multitenant mode. Consulted at the four seam points below (middleware, ownership,
# page/router mount-gate). Read at IMPORT time for the mount-gate; at REQUEST time
# for the middleware/ownership short-circuits.
from webview import webgate

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
from webview import posture_routes, template_globals

logger = logging.getLogger("webview.server")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

FEED_DEFAULT_LIMIT = max(1, int(os.environ.get('WEBVIEW_FEED_DEFAULT_LIMIT', '500')))
FEED_MAX_LIMIT = max(1, int(os.environ.get('WEBVIEW_FEED_MAX_LIMIT', '2000')))

logger.info(f"Feed event limits: default={FEED_DEFAULT_LIMIT}, max={FEED_MAX_LIMIT}")

# SECURITY FIX: Get allowed origins from environment
# Include the deploy's domain by default to prevent Socket.IO connection failures
# One domain SSOT with api/auth_endpoints.py's SIWE `domain` (which already reads
# Socket.IO CORS: the allowlist + the true-same-origin callable live in
# webview/cors.py (one concern, and server.py sits on its size ratchet). The
# module-level names are re-bound here because the Socket.IO server, the tests
# and the log line below resolve them off this module.
from webview.cors import (  # noqa: E402
    compute_cors_origins as _compute_cors_origins,
    make_origin_allowed as _make_origin_allowed)

_cors_origins = _compute_cors_origins(webgate.bind_port())
logger.info(f"Socket.IO CORS allowed origins: {_cors_origins}")
_cors_origin_allowed = _make_origin_allowed(_cors_origins)


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

# Process start time, for the public /api/status uptime figure (B2).
_START_TIME = time.monotonic()


def _api_base() -> str:
    """Base URL of the main task API this webview proxies to (chat delivery,
    queue-status, health) in the classic two-service shape. ``POLYROB_API_BASE``
    overrides the default loopback :9000 for non-default ports/hosts
    (2026-07-12 UI-surface review W5 — this was hardcoded at three sites)."""
    return (os.environ.get("POLYROB_API_BASE") or "http://127.0.0.1:9000").rstrip("/")


def _api_proxy_auth_headers(request: Request) -> Dict[str, str]:
    """Authentication for a WebView -> task-API server-side hop.

    The browser has already been authenticated by this process. In the classic
    two-service shape the downstream API must nevertheless validate the same
    credential; dropping it made every chat message look like an invalid token.
    Forward only the credential forms the API already understands, never
    arbitrary browser headers.

    A validated bearer wins, then the HttpOnly console cookie. ``API_AUTH_TOKEN``
    is the machine-client fallback for a local/API-key deployment. Values are
    deliberately never logged.
    """
    authorization = (request.headers.get("Authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return {"Authorization": authorization}
    cookie_token = (request.cookies.get("auth_token") or "").strip()
    if cookie_token:
        return {"Authorization": f"Bearer {cookie_token}"}
    api_token = (os.environ.get("API_AUTH_TOKEN") or "").strip()
    if api_token:
        return {"X-API-KEY": api_token}
    return {}


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


def _served_file_refusal(file_path) -> Optional[str]:
    """Refusal reason when a workspace file must never be SERVED (else None).

    Under project-root workspace mode the browsable tree is the whole project
    dir, which can legitimately contain a top-level ``.env`` — the traversal
    guards don't catch a plain credential-shaped basename (2026-07-19 review
    Minor #9). Fail-open on import error (guard unavailable ≠ outage)."""
    try:
        from pathlib import Path as _P
        from core.security.secret_guard import is_credential_file
        if is_credential_file(_P(str(file_path))):
            return "credential-shaped file"
    except ImportError:
        pass
    return None


def _workspace_mode_kwargs() -> dict:
    """Extra get_path_manager kwargs so the webview browses the SAME workspace
    root the agent writes to (QW-3, assessment 2026-07-19 §3.3).

    With POLYROB_PROJECT_DIR set the agent process (core/bootstrap) puts pm()
    into project-root mode — every session's workspace IS the project dir. The
    webview runs in its own process and never took that mode, so its file
    browser resolved the EMPTY per-session default. Gate: single-tenant
    postures only (local/own_ops) — in multitenant one shared project root
    would leak files cross-tenant.
    """
    try:
        if webgate.posture() == "multitenant":
            return {}
        env_project = (os.environ.get("POLYROB_PROJECT_DIR") or "").strip()
        if not env_project:
            return {}
        from pathlib import Path
        return {"workspace_is_project_root": True,
                "project_root": str(Path(env_project).resolve())}
    except Exception:
        logger.warning("workspace-mode resolution failed — using per-session "
                       "default", exc_info=True)
        return {}


@_fastapi.on_event("startup")
async def startup_event():
    """Initialize all services before accepting requests."""
    global _container

    logger.info("🚀 Initializing webview services...")

    # S8 (2026-09-14): refuse to boot an ANONYMOUS console ('local' posture =
    # every request is the owner) on a server-shaped deployment. Raises, so the
    # boot aborts rather than serving the control plane to the internet.
    from webview.posture_guard import assert_console_posture, assert_writable_console
    assert_console_posture()
    # W7 (043): a writable console must know WHOSE console it is — unbound, it
    # scopes every read and write to the instance id and renders honest-looking
    # empty lists. Read-only consoles are unaffected.
    assert_writable_console()

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
        _ws_mode = _workspace_mode_kwargs()
        set_path_manager(get_path_manager(data_root=str(_session_root), **_ws_mode))
        logger.info(f"✅ Session data root: {_session_root}"
                    + (f" (project-root workspace: {_ws_mode.get('project_root')})"
                       if _ws_mode else ""))
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
            "sandbox allow-scripts; "
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
        # CSP - strict. 043 R6: no `'unsafe-inline'` in `script-src` (de-inlined).
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.socket.io https://cdnjs.cloudflare.com https://fonts.googleapis.com https://cdn.jsdelivr.net; "
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

    # Raw workspace HTML/XML can also be navigated to directly, outside the
    # preview iframe. Keep every workspace document off the console origin.
    if (request.url.path.endswith("/workspace/file")
            or "/workspace/file/" in request.url.path):
        response.headers["Content-Security-Policy"] += "; sandbox"

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

# Rate limiting configuration (hardcoded for webview - not business logic)
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_CONNECTIONS = 10  # max connections per IP per window
RATE_LIMIT_MAX_EVENTS = 100  # max events per session per window
# Cap on distinct keys tracked per limiter: without a bound the per-session
# event limiter grew one key per session forever (E5-Minor), and the per-IP
# connection limiter one key per client IP. LRU eviction (max_keys) bounds the
# key space without changing rate-limit semantics for any key still active.
EVENT_EMISSIONS_MAX_SESSIONS = 5000
CONNECTION_ATTEMPTS_MAX_IPS = 5000

# Both throttles are configured instances of the canonical sliding-window
# primitive (F-1, 2026-07-17) — semantics pinned by
# tests/unit/webview/test_webview_rate_limits.py.
from core.rate_limit import SlidingWindowLimiter

_connection_limiter = SlidingWindowLimiter(
    RATE_LIMIT_MAX_CONNECTIONS, RATE_LIMIT_WINDOW, max_keys=CONNECTION_ATTEMPTS_MAX_IPS
)
_event_limiter = SlidingWindowLimiter(
    RATE_LIMIT_MAX_EVENTS, RATE_LIMIT_WINDOW, max_keys=EVENT_EMISSIONS_MAX_SESSIONS
)

def check_rate_limit(ip: str) -> bool:
    """Check if IP has exceeded connection rate limit.

    Args:
        ip: IP address to check

    Returns:
        True if within limit, False if exceeded
    """
    # Limits are passed per call so the module-level constants stay the live
    # config seam (tests monkeypatch them).
    if not _connection_limiter.check(ip, max_calls=RATE_LIMIT_MAX_CONNECTIONS,
                                     window=RATE_LIMIT_WINDOW):
        logger.warning(f"Rate limit exceeded for IP {ip}: {RATE_LIMIT_MAX_CONNECTIONS} connections in {RATE_LIMIT_WINDOW}s")
        return False
    return True

def check_event_rate_limit(session_id: str) -> bool:
    """Check if session has exceeded event emission rate limit.

    Args:
        session_id: Session ID to check

    Returns:
        True if within limit, False if exceeded
    """
    if not _event_limiter.check(session_id, max_calls=RATE_LIMIT_MAX_EVENTS,
                                window=RATE_LIMIT_WINDOW):
        logger.warning(f"Event rate limit exceeded for session {session_id}: {RATE_LIMIT_MAX_EVENTS} events in {RATE_LIMIT_WINDOW}s")
        return False
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


def _jti_revoked(jti: Optional[str]) -> bool:
    """True if this token's jti was revoked via /logout (fails closed on store error)."""
    try:
        from core.token_denylist import jti_is_revoked
        return jti_is_revoked({"jti": jti})
    except Exception:
        return True


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
                decoded = decode_session_token(auth_token, jwt_secret)
                require_session_identity(decoded)
                if _jti_revoked(decoded.get("jti")):
                    raise ValueError("token revoked via /logout")
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
        # (core/identity.py), which need not equal the own_ops owner-login id
        # (webgate.local_owner_id(): a BOUND owner, else "local"). A strict
        # per-session string match false-denies the owner on their CLI sessions.
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

# Every console-wide Jinja global (branding, version, posture defaults, auth
# state, the upload allowlist) lives in ONE module — see webview/template_globals.py.
template_globals.register(_templates, webgate)

#: Console routers that failed to mount, ``"<name>: <error>"`` each (W13).
#: The mounts fail OPEN so one broken page cannot take the whole console down —
#: which used to mean a console could boot with its control plane missing and
#: report it NOWHERE. Every failure lands here, is reported by
#: ``GET /api/webgate/doctor``, and in the `local` posture (a developer at a
#: terminal, no operator to page) is re-raised so the boot fails loudly.
UNMOUNTED_ROUTERS: List[str] = []


def _record_unmounted(name: str, exc: BaseException) -> None:
    UNMOUNTED_ROUTERS.append(f"{name}: {type(exc).__name__}: {exc}")
    logger.error("❌ Failed to mount the %s router: %s", name, exc)
    if webgate.is_local():
        raise exc


# Multitenant-only: the wallet auth router is the JWT/SIWE surface. In single-user
# mode it is simply not mounted (no /api/auth/* — single-user has no auth).
if webgate.is_multitenant():
    try:
        from api.auth_endpoints import router as auth_router
        _fastapi.include_router(auth_router, prefix="/api/auth",
                                tags=["authentication"],
                                dependencies=webgate.MUTATION_DEPS)
        AUTH_ROUTER_MOUNTED = True
        logger.info("✅ Auth endpoints mounted at /api/auth (nonce, verify, me)")
    except ImportError as e:
        AUTH_ROUTER_MOUNTED = False
        logger.warning("⚠️ Wallet authentication will not work!")
        _record_unmounted("auth", e)
else:
    AUTH_ROUTER_MOUNTED = False
    logger.info("webgate single-user mode: auth router not mounted (no JWT/SIWE)")

# WS-3.2 (2026-07-07) / W3 (043): the task + auth routers are mounted DIRECTLY
# in this app, so their mutating routes (POST /api/task/sessions, …/messages,
# …/cancel) would bypass the console's posture entirely. Their routes live in
# api/, outside this package's guard ratchet — so the console applies the ONE
# guard at the mount seam. Reads stay allowed.
try:
    from api.task_http_api import router as task_router
    _fastapi.include_router(
        task_router, prefix="/api", tags=["task"],
        dependencies=webgate.MUTATION_DEPS,
    )
    TASK_ROUTER_MOUNTED = True
    logger.info("✅ Task endpoints mounted at /api/task")
except ImportError as e:
    TASK_ROUTER_MOUNTED = False
    logger.warning("⚠️ Task session creation from webview will not work!")
    _record_unmounted("task", e)

PAYMENT_ROUTER_MOUNTED = False

# Webgate v1 read-only pages (Memory/Autonomy/Identity/System) — core single-user
# surfaces. Mounted in ALL postures; per-tenant scoping under multitenant is
# handled INSIDE each handler via webview.pages._effective_user_id (B7 —
# closes assessment gap 5, was previously hardcoded to the instance owner in
# every posture). Each endpoint REUSES the underlying service (memory
# provider / GoalBoard / CronService / core.instance / doctor_report); see
# webview/pages.py. Fail-open: a mount failure must never break the webview boot.
# 030 extraction: the telemetry fast-push + preview serve-token endpoints
# (every posture; internal_emit enforces localhost-only itself).
try:
    from webview.emit_api import router as _emit_api_router
    _fastapi.include_router(_emit_api_router)
except Exception as _e:
    _record_unmounted("emit_api", _e)

try:
    from webview.pages import router as webgate_pages_router
    _fastapi.include_router(webgate_pages_router)
    from webview.apps_routes import router as apps_router  # 032 durable app service
    _fastapi.include_router(apps_router)
    from webview.artifacts_api import router as artifacts_router  # 043 A16 Work-pane files
    _fastapi.include_router(artifacts_router)
    from webview.worklog_api import router as worklog_router  # 043 A16 Work › Log
    _fastapi.include_router(worklog_router)
    PAGES_ROUTER_MOUNTED = True
    logger.info("✅ Webgate v1 pages mounted (memory/autonomy/identity/system)")
except Exception as e:
    PAGES_ROUTER_MOUNTED = False
    _record_unmounted("webgate pages", e)

# /knowledge — the owner-facing knowledge wiki (C2, read-only v1: notes/episodes/
# skills/KB/changes). Same contract + tenancy as the pages router; fail-open mount.
try:
    from webview.knowledge import router as knowledge_router
    _fastapi.include_router(knowledge_router)
    KNOWLEDGE_ROUTER_MOUNTED = True
    logger.info("✅ Knowledge pages mounted (/knowledge)")
except Exception as e:
    KNOWLEDGE_ROUTER_MOUNTED = False
    _record_unmounted("knowledge", e)

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
    _record_unmounted("activity", e)

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

    # own_ops and multitenant always authenticate. Local posture has already
    # returned above; WEBVIEW_AUTH_ENABLED cannot disable a public control plane.
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
        "/api/internal/emit",  # 030 D12: fast-push; endpoint enforces localhost-only
    ]
    # Session URLs never confer read authority. Explicit scoped preview tokens
    # below are the only cookie-free artifact grant; feeds and files require owner auth.
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

    # 030 S1: the opaque-origin preview iframe sends no auth cookie — admit a
    # /serve/ request carrying a VALID token for exactly its own session.
    if path.startswith("/api/session/") and "/workspace/serve/" in path:
        st = request.query_params.get("st")
        sid = path[len("/api/session/"):].split("/", 1)[0]
        if st and sid and _verify_serve_token(sid, st):
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
        decoded = decode_session_token(auth_token, jwt_secret)
        require_session_identity(decoded)
        # 043 W5: a token whose jti /logout revoked is refused like a bad token.
        if _jti_revoked(decoded.get("jti")):
            raise pyjwt.InvalidTokenError("token revoked via /logout")

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

        from webview.session_access import may_read_session_path
        if not may_read_session_path(path, request.state.user_id, pm()):
            return JSONResponse(status_code=403, content={"error": "Session access denied"})

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

        # metadata.json is the ONLY place `creator` (043 A17) lives — read it
        # unconditionally (not just as a task_text fallback) — plus task/model/
        # provider fallbacks for whatever task.json/status.json didn't supply.
        creator = None
        metadata_file = session_path / "metadata.json"
        if metadata_file.exists():
            try:
                with metadata_file.open('r') as f:
                    metadata = json.load(f)
                    creator = metadata.get('creator')
                    if task_text == "No task description":
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
            'provider': provider or 'unknown',
            'creator': creator or 'api',
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
        # 019 P3: live activity badge (in-process RunActivity snapshot —
        # present only when THIS process runs the session; honest absence
        # otherwise). phase ∈ thinking|tool|awaiting_approval|compacting|
        # retrying|delegating; idle/done are omitted (no badge).
        try:
            from agents.task.telemetry.run_activity import get_activity
            activity = get_activity(row['id'])
            if activity and activity.get('phase') not in (None, '', 'idle', 'done'):
                row['activity'] = {
                    'phase': activity.get('phase'),
                    'detail': activity.get('detail') or '',
                }
        except Exception:
            pass
    return sessions


def _sessions_for_request(request: Request) -> List[Dict[str, Any]]:
    """Catalog rows for this request, per _catalog_scope."""
    scope, user_id = _catalog_scope(request)
    if scope == "all":
        return _annotate_runtime(_get_all_sessions())
    if scope == "user":
        return _annotate_runtime(_get_user_sessions(user_id=user_id))
    return []


@_posture_get("/logout", postures=("own_ops", "multitenant"), response_class=HTMLResponse)
async def logout(request: Request) -> Response:
    """Log out and return to the posture's login surface.

    Registered for own_ops AND multitenant (030 S5 — it was `_multitenant_get`,
    so the own_ops owner had NO way to end the ≤24h owner session: the route
    404'd there). own_ops answers with a real HTTP redirect + `delete_cookie`
    (the auth middleware's own rule: real redirects, never a 200 JS-hack page —
    the owner cookie is the only credential there). Multitenant keeps the
    legacy HTML page: the wallet/SIWE JWT also lives in localStorage, which
    only client-side JS can clear.
    """
    # 043 W5: revoke this token's jti server-side so the cookie cannot be
    # replayed after logout — delete_cookie only removes the browser's copy.
    from core.token_denylist import RevocationUnavailable, revoke_cookie_token
    try:
        await asyncio.to_thread(revoke_cookie_token, request.cookies.get("auth_token"))
    except RevocationUnavailable:
        return JSONResponse(status_code=503, content={
            "error": "Logout could not be saved. Please retry; your session has not been revoked."
        }, headers={"Retry-After": "5"})
    if webgate.is_own_ops():
        response: Response = RedirectResponse(url="/owner-login", status_code=303)
        response.delete_cookie("auth_token")
        return response
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
#: Most recently seen IPs kept. W6 (043): the bound used to be enforced with
#: `_login_attempts.clear()` — one address-churning attacker (trivially cheap
#: over IPv6, where a single /64 hands out billions) wiped the attempt history of
#: EVERY honest IP, including its own, and walked straight past the throttle.
#: An LRU evicts the OLDEST entry instead: a flood loses its own oldest records
#: and never the live ones.
_LOGIN_ATTEMPT_LRU_MAX = 10000
_login_attempts: "OrderedDict[str, list]" = OrderedDict()


def _login_throttled(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_ATTEMPT_WINDOW_SEC]
    if attempts:
        _login_attempts[ip] = attempts
        _login_attempts.move_to_end(ip)
    elif ip in _login_attempts:
        del _login_attempts[ip]   # expired window: drop the row, don't pin it
    return len(attempts) >= _LOGIN_ATTEMPT_MAX


def _record_login_attempt(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())
    _login_attempts.move_to_end(ip)
    while len(_login_attempts) > _LOGIN_ATTEMPT_LRU_MAX:
        _login_attempts.popitem(last=False)


# The form's own double-submit token and the return_to sanitizer live beside the
# render they protect (webview/owner_login_flow.py); aliased here because the
# routes and tests resolve them off this module.
from webview.owner_login_flow import (  # noqa: E402
    csrf_token_for as _csrf_token_for, safe_return_to as _safe_return_to)


# 030 S1: serve tokens live in webview/serve_tokens.py; private aliases kept —
# tests and the middleware resolve them off this module.
from webview.serve_tokens import (SERVE_TOKEN_TTL_SEC as _SERVE_TOKEN_TTL_SEC,
                                  mint_serve_token as _mint_serve_token,
                                  verify_serve_token as _verify_serve_token)


def _render_owner_login(request, *, return_to="/", error=None, status_code=200):
    """030 S5 delegator — body in webview/owner_login_flow.py (ratchet)."""
    from webview.owner_login_flow import render_owner_login
    return render_owner_login(request, return_to=return_to, error=error,
                              status_code=status_code)

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
    return_to = _safe_return_to(request.query_params.get("return_to", "/"))
    return _render_owner_login(request, return_to=return_to)


@_posture_post("/owner-login", postures=("own_ops", "multitenant"),
               dependencies=webgate.MUTATION_DEPS)
async def owner_login_submit(request: Request) -> Response:
    """Verify owner credentials and, on success, issue the owner session cookie.

    Same error for a bad username or a bad password (no user-enumeration).
    Hardened: per-IP attempt throttle (429), stateless double-submit CSRF
    (403 on mismatch when a JWT secret is configured), sanitized return_to.
    """
    from webview.owner_auth import verify_owner_password, issue_owner_session_cookie

    client_ip = request.client.host if request.client else "unknown"
    if _login_throttled(client_ip):
        return _render_owner_login(request,
            error="Too many attempts. Try again in a few minutes.", status_code=429)
    _record_login_attempt(client_ip)

    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    return_to = _safe_return_to(form.get("return_to", "/"))

    expected_csrf = _csrf_token_for(request.cookies.get("csrf_nonce"))
    if os.environ.get("JWT_SECRET_KEY"):
        supplied = str(form.get("csrf_token", ""))
        if not expected_csrf or not hmac.compare_digest(supplied, expected_csrf):
            return _render_owner_login(request, return_to=return_to,
                error="Invalid or expired form. Please try again.", status_code=403)

    if not verify_owner_password(username, password):
        return _render_owner_login(request, return_to=return_to,
            error="Invalid username or password.", status_code=401)

    response = RedirectResponse(url=return_to, status_code=303)
    issue_owner_session_cookie(response)
    return response


# The account + admin page surfaces (/signin, /profile, /admin*) are ONE posture
# table in webview/posture_routes.py (W13) — they were seven hand-registered
# routes that disagreed about what a denial looks like (/admin answered 403 with
# an inline-script alert page; its three siblings redirected). The legacy
# /settings page was deleted (043 §9 phase 4); the new Agent destination absorbs
# its panels.
posture_routes.mount(_fastapi, _templates)


@_fastapi.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    """Root fallback — the new chat shell serves `/` (043 phase 5).

    ``pages_new.mount`` unregisters this handler by name (``_drop_legacy_index``)
    and takes over `/`, where ``pages_new._public_visitor`` renders the public
    status page for an unauthenticated stranger and the chat for the owner. This
    handler survives only so a shell that fails to mount degrades to the public
    status page rather than a dead route (the legacy /sessions dashboard is
    deleted). It is never the primary `/` in a healthy console.
    """
    from core.instance import resolve_instance_id

    return _templates.TemplateResponse(request, "status.html", {
        "request": request,
        "instance_id": resolve_instance_id(),
        "version": os.environ.get("WEBVIEW_VERSION", get_version()),
    })


@_fastapi.get("/new", response_class=HTMLResponse)
async def new_session(request: Request) -> Response:
    """Alias for main chat page (backwards compatibility)."""
    return RedirectResponse(url="/", status_code=302)


@_fastapi.get("/session/{session_id}", response_class=HTMLResponse)
async def session_page(request: Request, session_id: str) -> Response:
    """Permanent redirect to the new chat (043 §9).

    The legacy session view (session.html) is deleted; `/c/{id}` is the one
    bound-session page. A 301 keeps an old bookmark or link landing on the
    session it named.
    """
    clean_id = pm().clean_session_id(session_id)
    return RedirectResponse(url=f"/c/{clean_id}", status_code=301)


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

    if _served_file_refusal(file_path):
        raise HTTPException(403, "Forbidden: credential-shaped file")

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

    if _served_file_refusal(file_path):
        raise HTTPException(403, "Forbidden: credential-shaped file")

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


@_fastapi.post("/api/repair/{session_id}", response_class=JSONResponse,
               dependencies=webgate.MUTATION_DEPS)
async def api_repair(request: Request, clean_id: str = Depends(get_clean_session_id)) -> Response:
    """Repair a session's telemetry (dedup + token estimation + validation).

    Wired to the REAL ``webview.repair_sessions.repair_session_telemetry``
    (this endpoint used to return fake success without doing anything).

    A POST since 043 W2. It rewrites the session's feed and llm_usage files, so
    as a GET it was a mutation any link, prefetch or crawler could trigger — and
    it sat outside both of this console's rules: the read-only guard passes GETs
    (a read-only console is still a console) and the CSRF check could not see it
    either. It now carries ``MUTATION_DEPS`` like every other mutation, on top of
    session ownership (SECURITY: previously unguarded — any authenticated tenant
    could mutate another tenant's telemetry).
    """
    is_owner, _current, _owner = _check_session_ownership(request, clean_id)
    if not is_owner:
        return JSONResponse(
            {"status": "error", "message": "Forbidden: not the session owner"},
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
        
        # Check if the screenshots directory exists. Creating it is a WRITE on a
        # GET (W13): a read-only console observes, it never materializes a
        # directory in the agent's session tree — so the side effect is
        # conditional, while the read itself stays allowed.
        if not screenshot_dir.exists() and not webgate.read_only():
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
        agents = build_session_agents(clean_id)
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
        services = build_session_services(clean_id)
        return JSONResponse(
            {"status": "ok", "services": services},
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
    """Return the current task for a session (the initial user task, not later goals)."""
    logger.debug(f"API task request for cleaned ID: {clean_id}")
    try:
        result = build_session_task(clean_id)
        if result is not None:
            return JSONResponse(
                {"status": "ok", "task": result["task"], "timestamp": result["timestamp"]},
                headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
            )
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
        skills = build_session_skills(clean_id)
        return JSONResponse(
            {"status": "ok", "skills": skills},
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
        'user_message', 'queue_status',  # Chat UI events
        'tool_execution', 'tool_result'  # 043 A16: the typed tool_result event
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
async def api_session_debug(session_id: str, request: Request) -> Response:
    """Debug endpoint to inspect session state and troubleshoot chat issues.

    Returns comprehensive information about:
    - Session directory structure
    - File existence (task.json, metadata.json, etc.)
    - Feed file counts by type
    - Sample feed files
    - Service connectivity status

    This endpoint is designed to help diagnose why the chat tab might be empty.

    SECURITY (W13): it dumps another tenant's session tree — absolute paths, a
    file inventory, feed samples — and carried NO ownership check. It now uses
    the same posture-aware authority as every other per-session route
    (``_check_session_ownership``: the local/own_ops owner owns every session;
    multitenant compares the authenticated caller against the session's owner).
    """
    clean_id = pm().clean_session_id(session_id)
    is_owner, current_user_id, _owner = _check_session_ownership(request, clean_id)
    if not is_owner:
        return JSONResponse(
            {"error": ("Authentication required" if current_user_id is None
                       else "Forbidden: not the session owner"),
             "session_id": session_id},
            status_code=401 if current_user_id is None else 403)
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
                api_resp = await client.get(_api_base() + "/health", timeout=2.0)
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


@_fastapi.post("/api/webview/sessions/{session_id}/stream",
               response_class=JSONResponse, dependencies=webgate.MUTATION_DEPS)
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


async def _maybe_handle_console_command(clean_id: str, user_id: str, text: str):
    """030 WS-B6 delegator — body in webview/console_commands.py (ratchet).
    Resolves the in-process agent HERE so tests can patch _in_process_task_agent."""
    from webview.console_commands import maybe_handle_console_command
    return await maybe_handle_console_command(
        _in_process_task_agent(), clean_id, user_id, text)


@_fastapi.post("/api/session/{session_id}/messages",
               response_class=JSONResponse, dependencies=webgate.MUTATION_DEPS)
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

        # 030 WS-B6 (Q1, finding G3): a slash VERB typed into the console chat
        # box used to be forwarded to the LLM as prose — /halt did not halt.
        # Route known owner verbs through the SAME handler chat surfaces use
        # and answer inline. Plain text (and unknown slashes) still reach the
        # agent unchanged.
        cmd_reply = await _maybe_handle_console_command(clean_id, current_user_id, text)
        if cmd_reply is not None:
            return JSONResponse({"success": True, "message": "Command handled",
                                 "command_reply": cmd_reply})

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
        api_url = "{}/api/task/sessions/{}/messages".format(_api_base(), clean_id)

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
                        headers=_api_proxy_auth_headers(request),
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
                        try:
                            upstream = response.json()
                        except Exception:
                            upstream = None
                        message = None
                        if isinstance(upstream, dict):
                            message = upstream.get("error") or upstream.get("message")
                            if not message and isinstance(upstream.get("detail"), str):
                                message = upstream["detail"]
                        return JSONResponse(
                            {"success": False,
                             "error": message or "The agent API refused the message"},
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
        api_url = f"{_api_base()}/api/task/sessions/{clean_id}/queue-status"

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

from webview.socket_auth import SocketAuthMonitor


async def _disconnect_expired_socket(sid):
    _socket_user.pop(sid, None)
    _socket_tier.pop(sid, None)
    await _sio.disconnect(sid)


_socket_auth_monitor = SocketAuthMonitor(_disconnect_expired_socket)


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
        payload = decode_session_token(token, jwt_secret) or {}
        require_session_identity(payload)
        from core.token_denylist import jti_is_revoked
        if jti_is_revoked(payload):
            return {}
        return payload
    except Exception as e:
        logger.debug(f"Socket.IO auth token decode failed: {e}")
        return {}


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
async def connect(sid: str, environ: Dict, auth: Dict | None = None) -> bool | None:  # noqa: D401 – Socket.IO callback
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
    if not isinstance(payload.get("user_id"), str) or not payload["user_id"]:
        # Socket.IO interprets False as a rejected namespace connection. Never
        # retain an anonymous connection on an authenticated console posture.
        return False
    _socket_user[sid] = payload.get("user_id")
    _socket_tier[sid] = payload.get("tier")
    _socket_auth_monitor.register(sid, payload)


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
    _socket_auth_monitor.remove(sid)
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

        # 030 WS-G2 (D-8): a RECONNECTING client that already holds state sends
        # after_seq — skip the full-feed replay (an unbounded glob + JSON parse
        # of the whole feed dir on every network blip) and let its delta-sync
        # (/feed/events?after_seq=) fill the gap. Room membership above is the
        # part a reconnect actually needs.
        try:
            _after_seq = int(data.get("after_seq") or 0)
        except (TypeError, ValueError):
            _after_seq = 0
        if _after_seq > 0:
            if clean_id not in _watch_tasks:
                _watch_tasks[clean_id] = asyncio.create_task(_feed_watcher(clean_id))
            logger.info("join_session: reconnect with after_seq=%d — skipping full replay", _after_seq)
            return

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
    global _container

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

                    # Create and register task agent.
                    # owns_workspace_gc=False: this is the read-only monitoring
                    # console. It has no business rmtree-ing the agent's
                    # workspaces, and it DID — a webview process started
                    # 2026-08-05 kept pre-guard code in memory and wiped
                    # /var/lib/polyrob/project every day at 06:26 UTC for two
                    # weeks, because deploys restart polyrob.service and not
                    # this one. The agent process owns the GC.
                    task_agent = TaskAgent(name="task_agent", config=config,
                                           container=_container,
                                           owns_workspace_gc=False)
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
    await _socket_auth_monitor.aclose()
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
    """Render a custom error page for HTTP errors — HTML for a PAGE, JSON for an API.

    W3 (043): the console answers every HTTPException with ``error.html``. That
    is right for ``/session/<id>`` in a browser tab and wrong for ``/api/…``,
    where the caller is ``fetch(…).then(r => r.json())`` — an HTML body turns a
    clear 403 ("Console is read-only") into a JSON parse error with no message.
    So an API path answers JSON. Both ``detail`` (FastAPI's convention, which
    ``chat.js`` already reads) and ``error`` (what the older page scripts read)
    carry the SAME string — one message, two readers, never two messages.
    """
    status_code = exc.status_code
    detail = exc.detail
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": detail, "error": detail},
                            status_code=status_code,
                            headers=getattr(exc, "headers", None) or None)
    
    # Get WebSocket URL from environment variable
    ws_url = os.environ.get("WEBVIEW_WS_URL", "")
    
    # Get version from environment variable or use a default
    version = os.environ.get("WEBVIEW_VERSION", get_version())
    
    return _templates.TemplateResponse(request, "error.html",
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

# --- the new console shell (043 C-series) ----------------------------------- #
# Mounted LAST, on purpose: webview/pages_new.py skips any path this app already
# serves, and that set is only complete after every registration above —
# including the console index `/`, whose decorator sits near the bottom of this
# module. Fail-open: the shell is additive. 043 phase 5 removed the WEBVIEW_UI
# switch and the legacy pages — this is the ONLY console now.
try:
    from webview.pages_new import mount as _mount_console_shell
    _mount_console_shell(_fastapi)
except Exception as _e:  # pragma: no cover — additive mount, never fatal
    logger.warning("new console shell not mounted: %s", _e)


# Register last so request bodies are bounded before authentication helpers or parsers.
from api.request_limits import RequestBodyLimitMiddleware
_fastapi.add_middleware(RequestBodyLimitMiddleware)
