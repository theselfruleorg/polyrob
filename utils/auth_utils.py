"""Authentication utilities for consistent user ID handling across the application.

This module provides a single source of truth for extracting authenticated user
information from requests, ensuring consistent behavior across API and webview.
"""

from typing import Optional
from fastapi import Request


def get_authenticated_user_id(request: Request) -> str:
    """Get the authenticated user ID from request state.

    This is the SINGLE SOURCE OF TRUTH for user identification across the app.
    Always use this function instead of accessing request.state.user_id directly.

    The user_id is set by authentication middleware after validating JWT tokens:
    - API: api/app.py auth middleware
    - Webview: webview/server.py auth middleware

    Args:
        request: FastAPI Request object with state set by auth middleware

    Returns:
        User ID string - either from JWT token or DEFAULT_USER_ID for unauthenticated

    Security:
        - User ID ALWAYS comes from verified JWT token (request.state)
        - NEVER accepts user_id from request body (client-controlled)
        - Falls back to DEFAULT_USER_ID for unauthenticated requests

    Example:
        ```python
        from utils.auth_utils import get_authenticated_user_id

        @router.post("/sessions")
        async def create_session(request: Request, ...):
            user_id = get_authenticated_user_id(request)
            # user_id is now guaranteed to be authenticated
        ```
    """
    # Get user_id from JWT token (set by auth middleware)
    user_id = getattr(request.state, 'user_id', None)

    # Fallback to DEFAULT_USER_ID if no authentication
    if not user_id:
        from agents.task.constants import DEFAULT_USER_ID
        user_id = DEFAULT_USER_ID

    return user_id


def get_user_tier(request: Request) -> str:
    """Get the user's tier from request state.

    Args:
        request: FastAPI Request object

    Returns:
        User tier string (free, holder, x402, admin)
    """
    tier = getattr(request.state, 'tier', None)
    if not tier:
        tier = "free"  # Default for unauthenticated users

    return tier


def get_user_wallet(request: Request) -> Optional[str]:
    """Get the user's wallet address from request state.

    Args:
        request: FastAPI Request object

    Returns:
        Wallet address string or None if not authenticated via wallet
    """
    return getattr(request.state, 'wallet_address', None)


def is_admin(request: Request) -> bool:
    """Check if the current user is an admin.

    Args:
        request: FastAPI Request object

    Returns:
        True if user has admin privileges
    """
    return getattr(request.state, 'is_admin', False)


def get_user_role(request: Request) -> str:
    """Get the user's role from request state.

    Args:
        request: FastAPI Request object

    Returns:
        Role string (user or admin)
    """
    return getattr(request.state, 'role', 'user')


def is_authenticated(request: Request) -> bool:
    """Check if the request is from an authenticated user.

    Args:
        request: FastAPI Request object

    Returns:
        True if user is authenticated (has valid JWT token)
    """
    return getattr(request.state, 'authenticated', False)
