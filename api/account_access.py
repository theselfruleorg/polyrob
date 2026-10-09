"""Apply the administrator's block list after every API authentication method."""
import logging

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


async def enforce_account_access(request, call_next):
    from api.auth_constants import is_public_path
    user_id = getattr(request.state, "user_id", None)
    if (not getattr(request.state, "authenticated", False) or not user_id
            or is_public_path(request.url.path)
            or user_id in {"api_user", "authenticated_api_user"}):
        return await call_next(request)
    try:
        from api.dependencies import require_service
        db = require_service("database_manager", missing="Account access unavailable")
        row = await db.fetch_one(
            "SELECT is_blocked FROM blocked_users WHERE user_id = ?", (user_id,))
    except Exception:
        logger.warning("Account block list is unreadable", exc_info=True)
        return JSONResponse({"error": "Account access unavailable"}, status_code=503)
    if row and row["is_blocked"]:
        return JSONResponse({"error": "Account blocked"}, status_code=403)
    return await call_next(request)
