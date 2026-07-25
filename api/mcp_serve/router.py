"""MCP-server ``/mcp`` JSON-RPC-over-POST router. Gated; mounted by
``api/app.py`` only when ``MCP_SERVE_ENABLED`` is on. Reuses POLYROB auth —
``get_user_permissive`` (accepts x402, JWT, and API-key auth), the same
permissive policy A2A uses (``api/a2a/endpoints.py``).

Hand-rolled JSON-RPC-over-POST — deliberately NOT built on the ``mcp`` PyPI
SDK (importable locally but never declared in ``requirements.txt``; this
surface must not depend on it). Reuses the in-repo JSON-RPC frame models
(``api/a2a/models.py``) rather than redefining request/response/error
shapes, and mirrors ``api/a2a/endpoints.py::a2a_rpc_endpoint``'s dispatch
ladder + exception-to-JSON-RPC-error mapping.

v1 is POST-only (no SSE/streamable transport) and read-only (no writes — see
the T2.3 plan's "Out of scope").
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from api.a2a.models import A2AErrorCode, JSONRPCError, JSONRPCRequest, JSONRPCResponse
from api.dependencies import get_user_permissive
from api.mcp_serve.handlers import MCPMethodNotFound, MCPUnknownTool, handle

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mcp-serve"])


def mcp_serve_enabled() -> bool:
    """Whether the MCP-server ``/mcp`` surface is mounted (default OFF)."""
    from core.env import bool_env

    return bool_env("MCP_SERVE_ENABLED", False)


def _get_container():
    from core.container import DependencyContainer

    return DependencyContainer.get_instance()


@router.post("/mcp")
async def mcp_endpoint(rpc_request: JSONRPCRequest, request: Request):
    """MCP JSON-RPC 2.0 endpoint.

    A request whose ``id`` is unset (``None``) is a JSON-RPC *notification*:
    per spec it never gets a response body, so on both success and failure we
    return a bare 200 with an empty body. Every other request always gets a
    ``JSONRPCResponse`` — ``result`` on success, ``error`` on failure (unknown
    method -> ``-32601``; auth failure mirrors A2A's
    ``HTTPException(401)`` -> ``AUTHENTICATION_REQUIRED`` mapping).
    """
    request_id = rpc_request.id
    is_notification = request_id is None

    try:
        user_id = await get_user_permissive(request)
        try:
            # Container lookup is fail-open at this layer: `initialize`/
            # `tools/list` need no container at all, and an app that hasn't
            # initialized the DI singleton (e.g. a bare unit-test app)
            # shouldn't crash those methods. `DependencyContainer.get_instance()`
            # raises exactly `ValueError` for that "not initialized yet" shape
            # (see core/container.py) — catch ONLY that; any other exception
            # is a real fault and must surface as INTERNAL_ERROR below, not be
            # silently swallowed into `container = None`. A `tools/call` body
            # that genuinely needs a container gets `None` here and raises its
            # own clear `isError` result (api/mcp_serve/handlers.py).
            container = _get_container()
        except ValueError:
            container = None
        result = await handle(
            method=rpc_request.method,
            params=rpc_request.params,
            user_id=user_id,
            container=container,
        )
    except MCPMethodNotFound as e:
        if is_notification:
            return Response(status_code=200)
        return JSONRPCResponse(
            id=request_id,
            error=JSONRPCError(code=A2AErrorCode.METHOD_NOT_FOUND, message=str(e)),
        )
    except MCPUnknownTool as e:
        # tools/call named a tool outside the 5 descriptors — a client/params
        # problem, not a store error, so it's a JSON-RPC error (not isError
        # content) — mirrors api/a2a/endpoints.py's ValueError -> INVALID_PARAMS.
        if is_notification:
            return Response(status_code=200)
        return JSONRPCResponse(
            id=request_id,
            error=JSONRPCError(code=A2AErrorCode.INVALID_PARAMS, message=str(e)),
        )
    except HTTPException as e:
        if is_notification:
            return Response(status_code=200)
        code = (
            A2AErrorCode.AUTHENTICATION_REQUIRED
            if e.status_code == 401
            else A2AErrorCode.INTERNAL_ERROR
        )
        return JSONRPCResponse(
            id=request_id, error=JSONRPCError(code=code, message=str(e.detail))
        )
    except Exception as e:
        logger.error(f"MCP RPC error: {e}", exc_info=True)
        if is_notification:
            return Response(status_code=200)
        # No data=str(e): raw exception text can carry DB paths/internals into
        # the HTTP response. The full traceback is already in the server log
        # (logger.error above); clients get the opaque code+message only.
        return JSONRPCResponse(
            id=request_id,
            error=JSONRPCError(code=A2AErrorCode.INTERNAL_ERROR, message="Internal error"),
        )

    if is_notification:
        return Response(status_code=200)
    return JSONRPCResponse(id=request_id, result=result)
