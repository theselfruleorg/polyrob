"""Apply the administrator's block list after every API authentication method.

``is_account_blocked`` is the ONE block predicate: the API middleware below,
wallet sign-in (``api/auth_endpoints.py``) and the multitenant console
(``webview/server.py::auth_middleware``) all read it.
"""
import logging

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


async def is_account_blocked(db, user_id) -> bool:
    """True when the administrator blocked *user_id*. Raises when the block list
    is unreadable: the caller refuses, it never reads that as "not blocked"."""
    row = await db.fetch_one(
        "SELECT is_blocked FROM blocked_users WHERE user_id = ?", (user_id,))
    return bool(row and dict(row).get("is_blocked"))


async def account_block_refusal(user_id):
    """None when *user_id* may go on; else the 403 (blocked) or 503 (unreadable) answer."""
    try:
        from api.dependencies import require_service
        db = require_service("database_manager", missing="Account access unavailable")
        blocked = await is_account_blocked(db, user_id)
    except Exception:
        logger.warning("Account block list is unreadable", exc_info=True)
        return JSONResponse({"error": "Account access unavailable"}, status_code=503)
    if blocked:
        return JSONResponse({"error": "Account blocked"}, status_code=403)
    return None


async def enforce_account_access(request, call_next):
    from api.auth_constants import is_public_path
    user_id = getattr(request.state, "user_id", None)
    if (not getattr(request.state, "authenticated", False) or not user_id
            or is_public_path(request.url.path)
            or user_id in {"api_user", "authenticated_api_user"}):
        return await call_next(request)
    refusal = await account_block_refusal(user_id)
    if refusal is not None:
        return refusal
    return await call_next(request)
