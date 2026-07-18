"""
FastAPI application factory for POLYROB platform.
Serves AutoV2 HTTP API endpoints.
"""

import os
import sys
import logging
import asyncio
from typing import Optional, Dict, Any, TYPE_CHECKING
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import time
from datetime import datetime
from dotenv import load_dotenv

# Set up project root
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Try to import sqlite3 replacement if needed
try:
    import pysqlite3
    sys.modules['sqlite3'] = pysqlite3
except ImportError:
    pass

# Import core components
from core.config import BotConfig
from core.container import DependencyContainer
from core.exceptions import AuthError, InsufficientCreditsError
from core.logging import setup_logging, get_component_logger
from core.version import get_version

# core.bot.Bot pulls core.initialization (the entire agent/LLM/embedder stack). It is only
# needed inside the lifespan (which builds the bot via build_server_bot), so it must NOT be
# imported at module load — that dragged torch/SDKs/agents into every `import api.app`
# (every uvicorn worker boot). See docs/plans/2026-06-26-runtime-architecture-finalization-FUSION.md (P0-S).
if TYPE_CHECKING:
    from core.bot import Bot as CoreBot

# Import Task HTTP router
from api.task_http_api import router as task_router

# Import Auth endpoints
try:
    from api.auth_endpoints import router as auth_router
    AUTH_ROUTER_AVAILABLE = True
except ImportError:
    AUTH_ROUTER_AVAILABLE = False

# Import API models and router for message handling
try:
    from api.models import MessageRequest, MessageResponse
    from api.conversation_manager import APIConversationManager
    from api.middleware import AuthenticationMiddleware, RateLimitMiddleware
    API_MODELS_AVAILABLE = True
except ImportError:
    API_MODELS_AVAILABLE = False

# Global references
app_state = {
    "bot": None,
    "container": None,
    "config": None,
    "logger": None,
    "active_updates": set(),  # Track active update IDs
    "update_semaphore": None,  # Limit concurrent updates
    "conversation_manager": None  # API conversation manager
}

# Background task for periodic cleanup
async def periodic_cleanup_task():
    """Run periodic cleanup tasks (nonces, expired sessions, etc.)."""
    import asyncio
    logger = app_state.get("logger")

    while True:
        try:
            await asyncio.sleep(300)  # Run every 5 minutes

            container = app_state.get("container")
            if not container:
                continue

            db = container.get_service("database")
            if not db:
                continue

            # Clean up expired nonces
            try:
                result = await db.execute("""
                    DELETE FROM auth_nonces
                    WHERE expires_at < datetime('now')
                """)
                if result.rowcount > 0:
                    logger.debug(f"Cleaned up {result.rowcount} expired nonces")
            except Exception as e:
                logger.debug(f"Nonce cleanup error (non-critical): {e}")

            # Clean up old audit log entries (keep 90 days)
            try:
                result = await db.execute("""
                    DELETE FROM user_mcp_audit_log
                    WHERE timestamp < datetime('now', '-90 days')
                """)
                if result.rowcount > 0:
                    logger.debug(f"Cleaned up {result.rowcount} old audit log entries")
            except Exception:
                pass  # Table may not exist

        except asyncio.CancelledError:
            logger.info("Periodic cleanup task cancelled")
            break
        except Exception as e:
            logger.warning(f"Error in periodic cleanup: {e}")


# Lifespan manager for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    logger = app_state["logger"]
    cleanup_task = None
    reap_task = None
    reap_stop_event = None
    autonomy_handles = None
    dispatcher = None

    try:
        # Startup
        logger.info("="*50)
        logger.info("🚀 FastAPI Application Starting")
        logger.info("="*50)

        # Initialize bot and container via shared bootstrap.
        # build_server_bot adds phase-6 server services (auth, billing,
        # payments) on top of the core phases 1-5 that build_bot does.
        from core.bootstrap import build_server_bot
        bot = await build_server_bot()

        app_state["config"] = bot.container.config
        app_state["bot"] = bot
        app_state["container"] = bot.container

        # Initialize update semaphore to limit concurrent processing
        # This prevents resource exhaustion from too many simultaneous updates
        # Increased from 5 to 50 for better concurrency handling
        max_concurrent = int(os.environ.get("MAX_CONCURRENT_UPDATES", "50"))
        app_state["update_semaphore"] = asyncio.Semaphore(max_concurrent)
        logger.info(f"Initialized update semaphore with {max_concurrent} concurrent updates limit")

        # Initialize API conversation manager
        if API_MODELS_AVAILABLE:
            try:
                conversation_manager = APIConversationManager()
                app_state["conversation_manager"] = conversation_manager

                logger.info("✅ Conversation manager initialized")
            except Exception as e:
                logger.warning(f"Could not initialize conversation manager: {e}")

        # x402 is now handled via fastapi-x402 middleware (no custom handler needed)

        logger.info("✅ Bot and container initialized")

        # Apply any pending schema migrations to bot.db BEFORE serving (C3), snapshotting
        # first if a real change is pending (C2). Idempotent + single-flight-locked +
        # fail-open: a migration failure logs loudly and leaves the DB on the inline
        # schema (prior behavior) rather than crashing startup. On the first engagement
        # it just baselines the already-at-HEAD schema; future migrations auto-apply here.
        try:
            from migrations.boot import run_boot_migrations
            mig = await run_boot_migrations(bot.container, local=False)
            if mig.get("applied"):
                logger.info("✅ Schema migrations applied at boot: %s", mig["applied"])
            elif mig.get("error"):
                logger.error("⚠️ Boot migration reported an error (serving anyway): %s", mig["error"])
        except Exception as e:
            logger.error("⚠️ Boot migration wiring failed (serving anyway): %s", e)

        # Log voice-transcription readiness at startup so a 'voice silently dropped'
        # deploy is immediately visible in the journal (Task 1.6 core-seam migration).
        try:
            from core.surfaces.transcription import log_transcription_readiness
            log_transcription_readiness(bot.container)
        except Exception as e:
            logger.debug("log_transcription_readiness unavailable: %s", e)

        # Start periodic cleanup task
        cleanup_task = asyncio.create_task(periodic_cleanup_task())
        logger.info("✅ Periodic cleanup task started")

        # Start the autonomy background loops (cron / goals / curator) via the
        # shared runtime — the SINGLE place both the FastAPI server and the CLI
        # REPL start them. Each loop is independently gated + fail-open; one loop
        # failing to build never blocks the others.
        from core.autonomy_runtime import start_autonomy
        autonomy_data_dir = getattr(bot.container.config, "data_dir", "data")
        autonomy_handles = start_autonomy(
            task_agent=bot.container.get_agent("task_agent"),
            data_dir=autonomy_data_dir,
        )

        dispatcher = bot.container.get_service("outbound_dispatcher")
        if dispatcher is not None:
            dispatcher.start()

        # Start session-registry reaper (P6) — opt-in via SESSION_REGISTRY_BACKEND=sqlite.
        # Inert for the default in-process registry (no reap_stale). Periodically prunes
        # rows whose last_seen_at is older than the ttl, so a worker that died without
        # remove() can't leave a dead-PID row that 409-loops routing forever. The ttl
        # (300s) is comfortably larger than the per-step heartbeat cadence.
        if os.getenv("SESSION_REGISTRY_BACKEND", "memory").strip().lower() == "sqlite":
            try:
                reap_task_agent = bot.container.get_agent("task_agent")
                reap_registry = getattr(reap_task_agent, "_registry", None)
                if reap_registry is not None and hasattr(reap_registry, "reap_stale"):
                    reap_stop_event = asyncio.Event()

                    async def _reap_loop(registry, stop_event):
                        REAP_INTERVAL = 60      # seconds between sweeps
                        REAP_TTL = 300          # seconds before a row is considered stale
                        while not stop_event.is_set():
                            try:
                                await asyncio.wait_for(stop_event.wait(), timeout=REAP_INTERVAL)
                                break  # stop event set
                            except asyncio.TimeoutError:
                                pass  # interval elapsed -> run a sweep
                            try:
                                reaped = registry.reap_stale(ttl_seconds=REAP_TTL)
                                if reaped:
                                    logger.info(f"Session registry reaped {len(reaped)} stale session(s)")
                            except Exception as e:
                                logger.warning(f"Session registry reap failed: {e}")

                    reap_task = asyncio.create_task(_reap_loop(reap_registry, reap_stop_event))
                    logger.info("✅ Session registry reaper started (SESSION_REGISTRY_BACKEND=sqlite)")
            except Exception as e:
                logger.warning(f"Could not start session registry reaper: {e}")

        logger.info("="*50)
        logger.info("✅ FastAPI Application Ready")
        logger.info("="*50)

        yield  # Server is running

    finally:
        # Shutdown
        logger.info("Shutting down FastAPI application...")

        # Cancel cleanup task
        if cleanup_task:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("Cleanup task stopped")

        # Stop the autonomy background loops (cron / goals / curator) via the
        # shared runtime. Idempotent + fail-open.
        if autonomy_handles is not None:
            await autonomy_handles.stop()

        # Stop the outbound dispatcher (fail-open).
        if dispatcher is not None:
            try:
                await dispatcher.stop()
            except Exception:
                pass

        # Stop session-registry reaper (P6)
        if reap_task:
            if reap_stop_event:
                reap_stop_event.set()
            reap_task.cancel()
            try:
                await reap_task
            except asyncio.CancelledError:
                pass
            logger.info("Session registry reaper stopped")

        # Cleanup bot
        if app_state["bot"]:
            await app_state["bot"].cleanup()
            logger.info("Bot cleanup completed")

        logger.info("FastAPI application shutdown complete")


async def fallback_auth_middleware(request: Request, call_next):
    """Fallback authentication for development when middleware not loaded.

    Lifted out of create_app()'s closure (C4) so it's independently
    unit-testable via `app.middleware("http")(fallback_auth_middleware)` —
    it only reads `os.environ` and `request`, no closure-captured state.
    """
    logger = app_state.get("logger") or logging.getLogger(__name__)
    path = request.url.path

    # Skip auth for health check, root, docs, test, and auth endpoints.
    # NOTE: "/" must be matched EXACTLY — as a startswith() prefix it matches
    # every path and would short-circuit the entire fallback (auth bypass).
    # NOTE: "/api/x402/requests" (the payable-invoice challenge + pay routes) must be
    # anonymous — a third-party payer has no POLYROB account. Payment authenticity is
    # enforced cryptographically by the facilitator, not by this gate.
    public_paths = ["/health", "/api/test-auth", "/api/auth", "/docs", "/redoc",
                    "/openapi.json", "/api/x402/requests", "/api/x402/pricing"]
    if path == "/" or any(path.startswith(p) for p in public_paths):
        return await call_next(request)

    # Only apply fallback auth if proper middleware not loaded
    if not getattr(request.state, "authenticated", False) and (
        path.startswith("/api/") or path.startswith("/task/")
    ):
        api_token = os.environ.get("API_AUTH_TOKEN")

        # Log the request for debugging
        logger.debug(f"🔍 Middleware processing: {request.method} {path}")

        # SECURITY: Require API_AUTH_TOKEN to be configured
        if not api_token or api_token.strip() == "":
            logger.error(f"🚫 SECURITY: API_AUTH_TOKEN not configured. Rejecting request to {path}")
            return JSONResponse(
                status_code=503,
                content={
                    "error": "Service misconfigured",
                    "detail": "Authentication not properly configured. Contact administrator."
                }
            )

        # Check X-API-KEY, Authorization Bearer headers, and auth_token cookie
        auth_header = request.headers.get("X-API-KEY")
        bearer_header = request.headers.get("Authorization")
        cookie_token = request.cookies.get("auth_token")

        provided_token = None
        is_jwt_token = False

        logger.debug(f"🔑 Auth headers for {path}: X-API-KEY={bool(auth_header)}, Authorization={bool(bearer_header)}, Cookie={bool(cookie_token)}")

        if auth_header:
            provided_token = auth_header
            # Also check if X-API-KEY contains a JWT
            is_jwt_token = auth_header.count('.') == 2
            logger.debug(f"🔑 Using X-API-KEY header, is_jwt={is_jwt_token}")
        elif bearer_header and bearer_header.startswith("Bearer "):
            provided_token = bearer_header[7:]  # Remove "Bearer " prefix
            # Check if this looks like a JWT (three base64 parts separated by dots)
            is_jwt_token = provided_token.count('.') == 2
            logger.debug(f"🔑 Using Bearer header, is_jwt={is_jwt_token}")
        elif cookie_token:
            # Check if cookie contains JWT token
            provided_token = cookie_token
            is_jwt_token = provided_token.count('.') == 2
            logger.debug(f"🔑 Using cookie, is_jwt={is_jwt_token}")

        if not provided_token:
            logger.warning(f"🔑 No auth token provided for {path}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Missing authentication",
                    "detail": "Provide X-API-KEY header, Authorization: Bearer token, or auth_token cookie"
                }
            )

        # Try to decode JWT token from wallet auth
        if is_jwt_token:
            try:
                import jwt
                jwt_secret = os.environ.get("JWT_SECRET_KEY")
                if jwt_secret:
                    decoded = jwt.decode(provided_token, jwt_secret, algorithms=["HS256"])
                    from api.auth_state import set_auth_state
                    set_auth_state(
                        request.state,
                        user_id=decoded.get("sub", "api_user"),
                        tier=decoded.get("tier", "free"),
                        role=decoded.get("role", "user"),
                        payment_method=None,
                        authenticated=True,
                    )
                    request.state.wallet_address = decoded.get("wallet")

                    # SECURITY: Use admin_wallet flag from JWT (set at login time)
                    # This is more secure than checking wallet list every request
                    is_admin_by_wallet = decoded.get("admin_wallet", False)

                    # Set admin flag from any ADMIN_ROLES role (e.g. 'admin',
                    # 'owner') OR admin_wallet flag in JWT. H1 fix: this used
                    # to hardcode `role == 'admin'` — a THIRD ad-hoc
                    # admin-truth path independent of core.constants.ADMIN_ROLES
                    # that would deny an owner-login session (role="owner").
                    from api.auth_constants import is_admin_role
                    is_admin_by_role = is_admin_role(request.state.role)

                    # Debug logging
                    logger.debug(f"🔍 Admin check: role={request.state.role}, admin_wallet_flag={is_admin_by_wallet}, is_admin_by_role={is_admin_by_role}")

                    if is_admin_by_role or is_admin_by_wallet:
                        request.state.is_admin = True
                        logger.debug(f"🔐 Admin access granted for role={request.state.role}")
                    else:
                        request.state.is_admin = False

                    logger.debug(f"JWT auth successful for user {request.state.user_id} (tier: {request.state.tier}, admin: {request.state.is_admin})")
                    return await call_next(request)
            except jwt.ExpiredSignatureError:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Token expired"}
                )
            except jwt.InvalidTokenError as e:
                logger.warning(f"Invalid JWT token: {e}")
                # Fall through to check if it's an API key
            except Exception as e:
                logger.error(f"JWT decode error: {e}")
                # Fall through to check if it's an API key

        # Check against API_AUTH_TOKEN (for non-JWT tokens)
        if provided_token != api_token:
            logger.warning(f"Invalid API key attempt for {path}")
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid API key"}
            )

        # Set authenticated user context for API key auth.
        # C4 fix: role="admin" must be set alongside tier="admin" — the admin
        # bypass in verify_payment_for_request reads `role` (via
        # extract_admin_info), not `tier`. Before this fix, a valid
        # API_AUTH_TOKEN request had tier="admin" but role defaulted to
        # 'user', so it fell through to an unconditional 402.
        from api.auth_state import set_auth_state
        set_auth_state(
            request.state,
            user_id="authenticated_api_user",
            tier="admin",
            role="admin",
            payment_method=None,
            authenticated=True,
        )

    return await call_next(request)


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""

    # Set up logging
    log_level = os.environ.get('LOG_LEVEL', 'INFO')
    setup_logging(log_level=log_level)
    logger = get_component_logger('api')
    app_state["logger"] = logger

    # Load environment via shared bootstrap
    from core.bootstrap import load_env
    env = load_env()
    logger.info(f"Using configuration environment: {env}")

    # Create FastAPI app with lifespan manager
    app = FastAPI(
        title="POLYROB Platform API",
        description="AutoV2 automation platform with HTTP API",
        version=get_version(),
        lifespan=lifespan
    )

    # CORS configuration
    # SECURITY FIX: Restrict CORS to specific origins and headers
    cors_origins_str = os.environ.get("CORS_ALLOW_ORIGINS", "http://localhost:3000")
    cors_origins = [origin.strip() for origin in cors_origins_str.split(',') if origin.strip()]

    # Define allowed headers explicitly instead of "*"
    # This prevents potential security issues with credential-based CORS
    allowed_headers = [
        "Accept",
        "Accept-Language",
        "Content-Language",
        "Content-Type",
        "Authorization",
        "X-API-KEY",
        "X-Requested-With",
        "X-Admin-Token",
        "Cache-Control",
    ]

    # Define allowed methods explicitly
    allowed_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"]

    # C3: register the fallback auth FIRST so Starlette (which inserts each
    # add_middleware at index 0) leaves it as the INNERMOST layer — it then runs
    # LAST, after AuthenticationMiddleware/JWTAuthMiddleware/X402 have authenticated
    # the request. Registering it last made it outermost and shadowed the DB-backed
    # rob_xxx API-key validator (self-service keys were rejected before it ran).
    app.middleware("http")(fallback_auth_middleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=allowed_methods,
        allow_headers=allowed_headers,
    )
    logger.info(f"CORS configured for origins: {cors_origins}")

    # Add rate limiting middleware
    if API_MODELS_AVAILABLE:
        try:
            rpm = int(os.environ.get("API_RATE_LIMIT_RPM", "60"))
            rph = int(os.environ.get("API_RATE_LIMIT_RPH", "1000"))
            burst = int(os.environ.get("API_RATE_LIMIT_BURST", "10"))
            app.add_middleware(
                RateLimitMiddleware,
                requests_per_minute=rpm,
                requests_per_hour=rph,
                burst_size=burst
            )
            logger.info(f"Rate limiting enabled: {rpm} RPM, {rph} RPH, {burst} burst")
        except Exception as e:
            logger.warning(f"Could not add rate limiting middleware: {e}")

    # Add authentication middleware
    if API_MODELS_AVAILABLE:
        try:
            secret_key = os.environ.get("API_SECRET", os.environ.get("ADMIN_TOKEN"))
            if secret_key:
                app.add_middleware(
                    AuthenticationMiddleware,
                    secret_key=secret_key
                )
                logger.info("Authentication middleware enabled")
        except Exception as e:
            logger.warning(f"Could not add authentication middleware: {e}")

    # Add JWT authentication middleware
    from api.jwt_middleware import JWTAuthMiddleware

    jwt_secret = os.environ.get("JWT_SECRET_KEY")
    if jwt_secret:
        # Validate JWT secret has minimum length for security
        if len(jwt_secret) < 32:
            logger.warning("⚠️ JWT_SECRET_KEY is shorter than 32 characters - consider using a stronger secret")
        app.add_middleware(JWTAuthMiddleware, jwt_secret=jwt_secret)
        logger.info("✅ JWT authentication middleware enabled")
    else:
        # SECURITY FIX: Check if we're in production - fail fast if so
        # In development, allow running without JWT for testing
        is_production = env == 'production' or os.environ.get('PRODUCTION', '').lower() == 'true'
        if is_production:
            logger.critical("🚨 FATAL: JWT_SECRET_KEY not set in production!")
            raise RuntimeError(
                "JWT_SECRET_KEY environment variable is required in production. "
                "Set it to a secure random string of at least 32 characters."
            )
        else:
            logger.warning("⚠️ JWT_SECRET_KEY not set - authentication disabled (development only)")

    # Add x402 payment middleware (runs BEFORE JWT middleware due to LIFO order)
    # This allows x402 payments to bypass JWT auth for pay-per-request access
    # Uses fastapi-x402 library for proper on-chain verification via Coinbase facilitator
    x402_enabled = os.environ.get("X402_ENABLED", "false").lower() == "true"
    if x402_enabled:
        try:
            from modules.x402.middleware import X402PaymentMiddleware, install_auth_state_writer
            from api.auth_state import set_auth_state

            # R-4 inversion: modules/x402 no longer imports api.auth_state; the
            # api tier installs the canonical C4 writer at mount time.
            install_auth_state_writer(set_auth_state)
            app.add_middleware(X402PaymentMiddleware, enabled=True)
            logger.info("✅ x402 payment middleware enabled (using fastapi-x402)")
        except ImportError as e:
            logger.warning(f"Could not add x402 middleware: {e}")

    # Global exception handlers
    @app.exception_handler(StarletteHTTPException)
    async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
        """Handle HTTP exceptions globally."""
        logger.error(f"HTTP exception: {exc.detail} | Path: {request.url.path}")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail}
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """Handle validation errors globally."""
        logger.error(f"Validation error: {exc.errors()} | Path: {request.url.path}")
        return JSONResponse(
            status_code=422,
            content={"error": "Validation failed", "details": exc.errors()}
        )

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError):
        """Translate auth/tier domain exceptions raised by modules/auth/.

        Status code comes from the exception class (e.g. TierError=403,
        UserNotFoundError=404). Keeps fastapi out of modules/auth/.
        """
        status = getattr(exc, 'status_code', 403)
        logger.warning(f"Auth error ({status}): {exc} | Path: {request.url.path}")
        return JSONResponse(status_code=status, content={"error": str(exc)})

    @app.exception_handler(InsufficientCreditsError)
    async def insufficient_credits_handler(request: Request, exc: InsufficientCreditsError):
        """Translate billing domain exception to HTTP 402."""
        logger.info(f"Insufficient credits: user={exc.user_id} required={exc.required} available={exc.available}")
        return JSONResponse(
            status_code=402,
            content={
                "error": str(exc),
                "user_id": exc.user_id,
                "required": exc.required,
                "available": exc.available,
            },
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """Global exception handler for unhandled errors."""
        error_id = f"error_{int(time.time() * 1000)}"

        # Log the full exception details
        logger.error(
            f"Unhandled exception {error_id} | "
            f"Path: {request.url.path} | "
            f"Method: {request.method} | "
            f"Error: {str(exc)}",
            exc_info=True
        )


        # Clean up active updates if present
        if hasattr(request.state, 'update_id'):
            app_state["active_updates"].discard(request.state.update_id)

        # Return a proper error response
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "error_id": error_id,
                "message": "An unexpected error occurred. Please try again later."
            }
        )

    # (fallback_auth_middleware is registered earlier — see C3 note above — so it
    # runs innermost/last, after the real auth middlewares.)

    # Health check endpoint with metrics
    @app.get("/health")
    async def health_check():
        """Health check endpoint with system metrics."""
        active_count = len(app_state.get("active_updates", set()))
        semaphore_available = app_state["update_semaphore"]._value if app_state.get("update_semaphore") else 0

        health_status = {
            "status": "healthy" if active_count < 40 else "degraded",
            "service": "rob-platform",
            "metrics": {
                "active_updates": active_count,
                "semaphore_available": semaphore_available,
                "bot_initialized": app_state.get("bot") is not None
            }
        }

        # Set appropriate status code
        status_code = 200 if health_status["status"] == "healthy" else 503
        return JSONResponse(content=health_status, status_code=status_code)

    # Mount Task API router on canonical path only
    app.include_router(task_router, prefix="/api")

    # Mount Auth router (wallet authentication)
    if AUTH_ROUTER_AVAILABLE:
        app.include_router(auth_router, prefix="/api/auth", tags=["authentication"])
        logger.info("✅ Auth endpoints registered at /api/auth")

    # Mount Payment router (deposits, credits, transactions)
    try:
        from api.payment_endpoints import router as payment_router
        app.include_router(payment_router, prefix="/api", tags=["payments"])
        logger.info("✅ Payment endpoints registered at /api/payments")
    except ImportError as e:
        logger.warning(f"Payment endpoints not available: {e}")

    # Mount x402 router (always register - endpoints self-check if x402 is configured)
    try:
        from api.x402_endpoints import router as x402_router
        app.include_router(x402_router, prefix="/api", tags=["x402"])
        x402_enabled = os.environ.get("X402_ENABLED", "false").lower() == "true"
        logger.info(f"✅ x402 endpoints registered at /api/x402 (payments {'enabled' if x402_enabled else 'info-only'})")
    except ImportError as e:
        logger.debug(f"x402 endpoints not available: {e}")

    # Mount OpenAI-compatible /v1 router (gated; default OFF). Reuses POLYROB auth +
    # per-request billing + the tool-light chat agent — multi-tenant-safe.
    try:
        from api.openai_compat.router import router as openai_compat_router, openai_compat_enabled
        if openai_compat_enabled():
            app.include_router(openai_compat_router)
            logger.info("✅ OpenAI-compatible endpoints registered at /v1")
        else:
            logger.debug("OpenAI-compatible /v1 endpoints disabled (OPENAI_COMPAT_API_ENABLED off)")
    except ImportError as e:
        logger.warning(f"OpenAI-compatible endpoints not available: {e}")

    # Mount KB (knowledge-base) /api/kb router (gated; default OFF). Reuses POLYROB
    # auth (get_user_id dependency) — multi-tenant-safe; user_id never from body.
    try:
        from api.kb.endpoints import router as kb_router, kb_api_enabled
        if kb_api_enabled():
            app.include_router(kb_router)
            logger.info("✅ KB endpoints registered at /api/kb")
        else:
            logger.debug("KB endpoints disabled (KB_API_ENABLED off)")
    except ImportError as e:
        logger.warning(f"KB endpoints not available: {e}")

    # Mount Admin router
    try:
        from api.admin_endpoints import router as admin_router
        app.include_router(admin_router, prefix="/api", tags=["admin"])
        logger.info("✅ Admin endpoints registered at /api/admin")
    except ImportError as e:
        logger.warning(f"Admin endpoints not available: {e}")

    # Mount MCP management router
    try:
        from api.mcp_routes import router as mcp_router
        app.include_router(mcp_router, prefix="/api/mcp", tags=["mcp"])
        logger.info("✅ MCP endpoints registered at /api/mcp")
    except ImportError as e:
        logger.warning(f"MCP endpoints not available: {e}")

    # Mount Polymarket router
    try:
        from api.polymarket_routes import router as polymarket_router
        app.include_router(polymarket_router, prefix="/api/polymarket", tags=["polymarket"])
        logger.info("✅ Polymarket endpoints registered at /api/polymarket")
    except ImportError as e:
        logger.warning(f"Polymarket endpoints not available: {e}")

    # Mount Hyperliquid router
    try:
        from api.hyperliquid_routes import router as hyperliquid_router
        app.include_router(hyperliquid_router, tags=["hyperliquid"])
        logger.info("✅ Hyperliquid endpoints registered at /api/hyperliquid")
    except ImportError as e:
        logger.warning(f"Hyperliquid endpoints not available: {e}")

    # Mount Skills management router
    try:
        from api.skill_endpoints import router as skill_router
        app.include_router(skill_router, tags=["skills"])
        logger.info("✅ Skills endpoints registered at /api/skills")
    except ImportError as e:
        logger.warning(f"Skills endpoints not available: {e}")

    # Mount Pricing router (transparent token-based pricing)
    try:
        from api.pricing_endpoints import router as pricing_router
        app.include_router(pricing_router, prefix="/api")
        logger.info("✅ Pricing endpoints registered at /api/pricing")
    except ImportError as e:
        logger.warning(f"Pricing endpoints not available: {e}")

    # Mount A2A (Agent-to-Agent) Protocol routers
    try:
        from api.a2a.agent_card import router as a2a_card_router
        from api.a2a.endpoints import router as a2a_endpoints_router
        from api.a2a.streaming import router as a2a_streaming_router

        # Agent Card at well-known path (RFC 8615) - no prefix
        app.include_router(a2a_card_router, tags=["a2a-discovery"])

        # A2A API endpoints
        app.include_router(a2a_endpoints_router, tags=["a2a"])
        app.include_router(a2a_streaming_router, tags=["a2a-streaming"])

        logger.info("✅ A2A Protocol endpoints registered:")
        logger.info("   - Agent Card: /.well-known/agent.json")
        logger.info("   - JSON-RPC: /a2a/rpc")
        logger.info("   - REST API: /a2a/tasks/*")
        logger.info("   - Streaming: /a2a/message/stream, /a2a/tasks/*/stream")
    except ImportError as e:
        logger.warning(f"A2A endpoints not available: {e}")

    # Mount ERC-8004 Trustless Agents router
    try:
        from api.eip8004_endpoints import router as eip8004_router
        app.include_router(eip8004_router, tags=["eip8004-trustless-agents"])
        
        eip8004_enabled = os.environ.get("EIP8004_ENABLED", "false").lower() == "true"
        logger.info(f"✅ ERC-8004 Trustless Agents endpoints registered at /eip8004")
        logger.info(f"   - Registration: /eip8004/registration.json")
        logger.info(f"   - Reputation: /eip8004/reputation/*")
        logger.info(f"   - Validation: /eip8004/validation/*")
        logger.info(f"   - Status: {'enabled' if eip8004_enabled else 'discovery-only'}")
    except ImportError as e:
        logger.warning(f"ERC-8004 endpoints not available: {e}")

    # Mount inbound webhook router (GET verify handshake + POST delegate to WebhookSurface).
    # Empty registry → 404s; mounting is always safe (additive, fail-open).
    try:
        from api.webhooks import router as webhooks_router, set_container_provider
        set_container_provider(lambda: app_state.get("container"))
        app.include_router(webhooks_router, tags=["webhooks"])
        logger.info("✅ Inbound webhooks registered at /webhooks/{surface_id}")
    except Exception as e:
        logger.warning("webhooks router not mounted: %s", e)

    # Canonical public chat endpoint — served by the unified task agent (chat_once)
    if API_MODELS_AVAILABLE:
        @app.post("/api/chat/message", response_model=MessageResponse)
        async def send_chat_message(request: MessageRequest, req: Request):
            """Public HTTP chat endpoint — served by the unified task agent.

            The legacy ChatAgent was retired (HANDOFF-C); chat is now handled
            solely by ``TaskAgent.chat_once`` via the canonical handler, which
            returns a graceful ``MessageResponse(success=False)`` on failure.
            """
            try:
                container = app_state.get("container")
                if not container:
                    raise HTTPException(status_code=503, detail="Container not initialized")
                # SECURITY (C1): identity comes ONLY from the authenticated request
                # state, never from the client-supplied body. Trusting request.user_id
                # let any authenticated caller bill/recall memory as another tenant.
                from utils.auth_utils import get_authenticated_user_id
                user_id = get_authenticated_user_id(req)

                from api.chat_via_task import handle_chat_via_task_agent
                return await handle_chat_via_task_agent(
                    container, user_id, request.text, request.chat_id or user_id,
                )
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error processing chat message: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        # Legacy endpoint redirect
        @app.post("/api/message", response_model=MessageResponse)
        async def legacy_send_message(request: MessageRequest, req: Request):
            """Legacy message endpoint - redirects to /api/chat/message."""
            # For now, keep the same functionality but log the usage
            logger.info("Legacy /api/message endpoint used, consider migrating to /api/chat/message")
            return await send_chat_message(request, req)

    # Removed legacy alias routes. Use canonical /api/task/* endpoints via included router.

    # API Key Management Endpoints (admin only)
    @app.post("/api/admin/generate-key")
    async def generate_api_key(admin_token: str = Header(None, alias="X-Admin-Token")):
        """Generate a new API key (requires admin token)."""
        import secrets
        import hashlib

        # Check admin authorization
        expected_admin_token = os.environ.get("ADMIN_TOKEN")
        if not expected_admin_token:
            return JSONResponse(
                status_code=503,
                content={"error": "Admin authentication not configured"}
            )

        if not admin_token or admin_token != expected_admin_token:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized admin access"}
            )

        # Generate a secure API key
        api_key = secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:8]

        return {
            "api_key": api_key,
            "key_id": f"key_{key_hash}",
            "created_at": datetime.now().isoformat(),
            "note": "Store this key securely. It won't be shown again."
        }

    @app.get("/api/test-auth")
    async def test_auth(x_api_key: Optional[str] = Header(None)):
        """Test endpoint to verify API key authentication."""
        api_token = os.environ.get("API_AUTH_TOKEN")

        return {
            "authenticated": bool(x_api_key and api_token and x_api_key == api_token),
            "has_key": bool(x_api_key),
            "auth_configured": bool(api_token),
            "message": "Authentication test endpoint"
        }

    return app

# Factory function for uvicorn
def get_app() -> FastAPI:
    """Get FastAPI application instance for uvicorn."""
    return create_app()

# For development/testing. Prefer `python main.py`, which already gates
# reload behind UVICORN_RELOAD (default false) -- this direct-run path now
# matches it instead of hardcoding a file-watcher that polls the whole repo
# tree even when idle.
if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(
        "api.app:get_app",
        host="127.0.0.1",
        port=9000,
        reload=os.environ.get("UVICORN_RELOAD", "false").lower() == "true",
        factory=True
    )