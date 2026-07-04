"""Consolidated FastAPI dependencies for auth, identity, and orchestrator resolution.

This module is the SINGLE source of truth for:
- ``get_user_id``        — canonical identity extraction from request state
- ``get_client_ip``      — client IP extraction with consistent default "unknown"
- ``get_user_strict``    — STRICT auth policy: rejects api_user / authenticated_api_user
                           (mirrors payment_endpoints.get_authenticated_user verbatim)
- ``get_user_permissive``— PERMISSIVE auth policy: accepts x402, wallet-derivation, API keys
                           (mirrors api/a2a/endpoints.get_authenticated_user verbatim)
- ``resolve_orchestrator``— clean_session_id → guard_remote → get_orchestrator pipeline

Security note:
    The two auth policies are INTENTIONALLY different and must stay distinct:
    - ``get_user_strict`` is used by payment and billing endpoints.
    - ``get_user_permissive`` is used by A2A protocol endpoints.
    Do NOT conflate them; doing so silently changes auth behavior.

``get_client_ip`` default: "unknown" (str, never None).
    Reason: ``hyperliquid_routes`` used "unknown" as a string default,
    matching the audit-log convention for unknown clients.  ``mcp_routes``
    and ``polymarket_routes`` returned ``None`` (Optional[str]), which could
    propagate into audit tables as NULL.  The string "unknown" is safest.
"""

from typing import Optional

from fastapi import HTTPException, Request

from agents.task.path import pm
from api.session_routing import guard_remote


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------


def get_user_id(request: Request) -> str:
    """Extract authenticated user ID from request state.

    The auth middleware (``AuthenticationMiddleware`` / ``JWTAuthMiddleware``)
    is responsible for setting ``request.state.user_id``.  This dependency
    trusts that value unconditionally — it does NOT re-validate the token.

    Raises:
        HTTPException 401: if ``user_id`` is absent or ``None``.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="User not authenticated")
    return user_id


def get_client_ip(request: Request) -> str:
    """Extract the client's IP address.

    Precedence:
    1. First value in the ``X-Forwarded-For`` header (proxy / load-balancer path).
    2. ``request.client.host`` (direct connection).
    3. ``"unknown"`` — canonical string default when no IP is available.

    Returns:
        str, never ``None`` — callers (audit logs, rate limiters) can store it
        directly without a null-check.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# ---------------------------------------------------------------------------
# Auth policies
# ---------------------------------------------------------------------------


async def get_user_strict(request: Request) -> str:
    """STRICT auth policy — rejects synthetic / API-key placeholder identities.

    Used by: payment_endpoints, billing-sensitive endpoints.

    Mirrors ``payment_endpoints.get_authenticated_user`` verbatim.  The
    rejected IDs (``api_user``, ``authenticated_api_user``) are set by
    ``app.py`` middleware for API-token auth and are intentionally NOT
    accepted here — those callers must sign in with a wallet to obtain a
    real ``user_id``.

    Raises:
        HTTPException 401: if no real user_id is present.
    """
    user_id = getattr(request.state, "user_id", None)

    if not user_id or user_id in ("api_user", "authenticated_api_user"):
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please sign in with your wallet at /signin",
        )

    return user_id


async def get_user_permissive(request: Request) -> str:
    """PERMISSIVE auth policy — accepts x402 payment, JWT, and API-key auth.

    Used by: A2A protocol endpoints.

    Mirrors ``api/a2a/endpoints.get_authenticated_user`` verbatim.  Unlike
    ``get_user_strict``, this policy accepts ``authenticated_api_user`` so
    that machine-to-machine A2A clients authenticating via API key are not
    blocked.

    Auth resolution order (matches original A2A logic exactly):
    1. x402 payment (``request.state.payment_method == "x402"``)
       a. Use ``request.state.user_id`` if already set by middleware.
       b. Derive from ``request.state.payer_address`` via
          ``generate_user_id_from_wallet``.
       c. Fall back to the literal string ``"x402_user"``.
    2. JWT user_id (``request.state.user_id``) if it is not ``"api_user"``.
    3. Authenticated API-key session (``request.state.authenticated == True``).

    Raises:
        HTTPException 401: if none of the above paths resolves a user.
    """
    # Path 1 — x402 payment
    if (
        hasattr(request.state, "payment_method")
        and request.state.payment_method == "x402"
    ):
        user_id = getattr(request.state, "user_id", None)
        if user_id:
            return user_id
        # Fallback to wallet-derived ID if middleware didn't set it
        from core.identity import generate_user_id_from_wallet

        payer_address = getattr(request.state, "payer_address", None)
        if payer_address:
            return generate_user_id_from_wallet(payer_address)
        return "x402_user"

    # Path 2 — JWT user_id (not the synthetic "api_user" placeholder)
    user_id = getattr(request.state, "user_id", None)
    if user_id and user_id != "api_user":
        return user_id

    # Path 3 — authenticated via API key
    if getattr(request.state, "authenticated", False):
        return getattr(request.state, "user_id", "authenticated_api_user")

    raise HTTPException(
        status_code=401,
        detail="A2A authentication required (x402 payment, Bearer token, or API key)",
    )


# ---------------------------------------------------------------------------
# Orchestrator resolution
# ---------------------------------------------------------------------------


async def resolve_orchestrator(session_id: str, agent) -> Optional[object]:
    """Canonical orchestrator-resolution pipeline.

    Steps (must be performed in this order):
    1. ``pm().clean_session_id(session_id)`` — normalise the raw path segment
       (strip unsafe chars, enforce known prefixes).
    2. ``guard_remote(agent, cleaned_id)`` — raise ``HTTPException 409`` if the
       session is owned by another uvicorn worker (honest-409, not false-404).
    3. ``agent.get_orchestrator(cleaned_id)`` — return the live in-process
       orchestrator or ``None`` if the session is missing / not yet running.

    Args:
        session_id: Raw session ID from the URL path parameter.
        agent: ``TaskAgentLite`` instance (from ``get_task_agent`` dependency).

    Returns:
        The live orchestrator, or ``None`` if the session is LOCAL-but-absent
        or MISSING (callers can apply their own DB-backed resumption logic).

    Raises:
        HTTPException 409: if the session is owned by another worker (REMOTE).
    """
    cleaned_id = pm().clean_session_id(session_id)
    guard_remote(agent, cleaned_id)
    return agent.get_orchestrator(cleaned_id)
