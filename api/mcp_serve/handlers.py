"""MCP-server JSON-RPC method ladder — the inbound read-only Model Context
Protocol surface behind ``MCP_SERVE_ENABLED`` (T2.3).

This is the OPPOSITE direction from ``tools/mcp/`` (which makes Rob an MCP
*client* of external servers): here Rob itself is the MCP *server*, so an
external client (Claude Desktop, Cursor) can point at ``POST /mcp`` with a
Rob API key and call ``initialize`` / ``tools/list`` / ``tools/call``.

Router/handler split mirrors ``api/a2a/endpoints.py``'s ``_handle_rpc_method``:
auth + JSON-RPC framing stay in ``router.py``; this module is pure method
logic (no FastAPI/``Request`` objects), so it is trivial to unit test
standalone.

Method ladder:
- ``initialize`` — protocol handshake; echoes our own protocolVersion
  honestly (never the client's, even if newer/unknown).
- ``notifications/initialized`` — accepted; returns ``None`` (a JSON-RPC
  *notification* — the router sends no response body per spec).
- ``tools/list`` — the 5 tenant-safe read-only tool descriptors (name +
  description + a hand-written minimal JSON-Schema ``inputSchema`` — no
  schema-generator dependency).
- ``tools/call`` — Task 2: real dispatch over the 5 tools (replaces Task 1's
  "not yet wired" stub). An unrecognized tool NAME is a protocol-level
  problem — :class:`MCPUnknownTool`, mapped by the router to JSON-RPC
  ``INVALID_PARAMS`` (-32602), mirroring ``api/a2a/endpoints.py``'s
  ``ValueError`` -> ``INVALID_PARAMS`` precedent. Bad/missing PARAMS for an
  otherwise-known tool, or a backing store that isn't reachable yet, stay
  INSIDE the JSON-RPC success envelope as ``isError: true`` content — a real
  MCP client's tool-call UI renders that gracefully instead of the whole
  call failing.
- anything else — raises :class:`MCPMethodNotFound`; the router maps that to
  a JSON-RPC ``-32601`` (METHOD_NOT_FOUND) error response.

Tenancy (see the T2.3 plan's backing-call table): every store call is scoped
to the authenticated ``user_id`` the router resolved via
``get_user_permissive`` — never a caller-supplied id. ``rob_goals_list``
ALWAYS passes ``user_id`` to ``GoalBoard.list`` (``user_id=None`` there would
be a cross-tenant read). ``rob_goal_show`` fetches by id (which IS
cross-tenant-readable at the storage layer) and then post-filters
``goal.user_id == user_id``; a mismatch and a genuinely-missing id produce
the EXACT SAME "not found" message — there is no "forbidden" outcome, so a
caller can never use this tool as an existence oracle for another tenant's
goals.

Keep ALL heavy/domain imports lazy inside handler bodies (entry-point import
hygiene — ``tests/test_import_layering.py``).
"""
import json
import logging
from typing import Any, Dict, List, Optional

from core.version import get_version

logger = logging.getLogger(__name__)

#: Echoed verbatim in `initialize` — Rob's own protocol version, never the
#: client's requested one (honest, even if the client asks for something
#: newer/unknown).
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "polyrob"


class MCPMethodNotFound(Exception):
    """Unrecognized JSON-RPC method; the router maps this to -32601."""

    def __init__(self, method: str):
        super().__init__(f"Method '{method}' not found")
        self.method = method


class MCPUnknownTool(Exception):
    """``tools/call`` named a tool outside :data:`TOOL_NAMES`.

    Kept as its own type (rather than a bare ``ValueError``) so the router's
    exception ladder can map exactly this case to JSON-RPC ``INVALID_PARAMS``
    without also reclassifying an unrelated ``ValueError`` raised deeper in a
    tool body (e.g. a genuine bug) as a client-params problem — that stays an
    ``INTERNAL_ERROR``.
    """

    def __init__(self, name: Any):
        super().__init__(f"Unknown tool: {name!r}")
        self.name = name


class _ToolUnavailable(Exception):
    """A tool body's backing store isn't reachable right now (e.g. the DI
    container hasn't initialized yet). Caught inside :func:`_tools_call` and
    turned into ``isError`` content — never a hard JSON-RPC failure, and
    never a silent fallback to writing/reading some OTHER store than the one
    the running server actually uses."""


# ---------------------------------------------------------------------------
# Tool descriptors (v1, read-only, tenant-safe by construction — see the
# T2.3 plan's backing-call table). Real dispatch lands in Task 2; these are
# hand-written minimal JSON-Schema ``inputSchema`` dicts.
# ---------------------------------------------------------------------------
TOOL_DESCRIPTORS: List[Dict[str, Any]] = [
    {
        "name": "rob_usage_summary",
        "description": (
            "Summarize the caller's LLM usage (API cost, credits, call count), "
            "optionally narrowed to one session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Narrow the rollup to one session (optional).",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rob_goals_list",
        "description": "List the caller's autonomy goals, optionally filtered by status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by goal status (e.g. 'open', 'done', 'blocked').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (capped at 50).",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rob_goal_show",
        "description": "Show one of the caller's goals by id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "The goal id."},
            },
            "required": ["goal_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rob_conversations",
        "description": "List the caller's recent correspondent conversations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max conversations to return (capped at 100).",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rob_pending_approvals",
        "description": "List the caller's pending tool-approval requests.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]

#: Names of the tool descriptors above, for the Task 2 dispatch table / any
#: "known tool" checks.
TOOL_NAMES = frozenset(t["name"] for t in TOOL_DESCRIPTORS)


def _initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "serverInfo": {"name": SERVER_NAME, "version": get_version()},
        "capabilities": {"tools": {}},
    }


def _tools_list(params: Dict[str, Any]) -> Dict[str, Any]:
    return {"tools": TOOL_DESCRIPTORS}


# ---------------------------------------------------------------------------
# tools/call (Task 2) — the five tool bodies + tenant-scoped dispatch.
# ---------------------------------------------------------------------------

def _text_result(payload: Any) -> Dict[str, Any]:
    """Structured success — ``payload`` is JSON-encoded into the single text
    content block (a plain ``str`` payload is passed through verbatim)."""
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _error_result(message: str) -> Dict[str, Any]:
    """Bad params / degraded-store outcome — stays inside the JSON-RPC
    success envelope as ``isError: true`` content (see module docstring)."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _goal_board(container: Any):
    """Construct a :class:`GoalBoard` against the SAME ``goals.db`` the rest
    of the app uses — mirrors ``tools/goal_tools.py:195``'s construction
    (``GoalBoard(goals_db_path(data_dir))``).

    Raises :class:`_ToolUnavailable` when there's no container to source
    ``data_dir`` from, rather than silently falling back to the process's
    default data home — a not-yet-initialized DI singleton (``container is
    None``, see ``router.py::mcp_endpoint``) must degrade to an honest
    isError, never a surprise read/write against some OTHER data directory
    than the one the running server actually uses.
    """
    if container is None:
        raise _ToolUnavailable("goal board unavailable (server not fully initialized)")
    from agents.task.goals.board import GoalBoard
    from core.runtime_paths import goals_db_path

    data_dir = getattr(getattr(container, "config", None), "data_dir", None)
    return GoalBoard(goals_db_path(data_dir))


def _goal_summary(goal: Any) -> Dict[str, Any]:
    return {
        "id": goal.id,
        "title": goal.title,
        "body": goal.body,
        "status": goal.status,
        "kind": goal.kind,
        "priority": goal.priority,
        "result": goal.result,
        "created_at": goal.created_at,
        "completed_at": goal.completed_at,
    }


async def _call_usage_summary(
    arguments: Dict[str, Any], user_id: str, container: Any
) -> Dict[str, Any]:
    session_id = arguments.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        return _error_result("session_id must be a string")
    from modules.credits.usage_rollup import usage_rollup

    # `usage_rollup` resolves its own db via the process DI singleton and is
    # REQUIRED to be given `user_id` for a non-zero result (fails open to an
    # all-zero rollup for a falsy/anonymous tenant or a missing db service) —
    # mirrors every other in-repo call site (e.g.
    # tools/controller/action_registration.py's `usage_summary` action).
    rollup = await usage_rollup(user_id, session_id=session_id)
    return _text_result(rollup)


async def _call_goals_list(
    arguments: Dict[str, Any], user_id: str, container: Any
) -> Dict[str, Any]:
    status = arguments.get("status")
    if status is not None and not isinstance(status, str):
        return _error_result("status must be a string")
    limit = arguments.get("limit", 50)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return _error_result("limit must be an integer")
    if limit < 1:
        return _error_result("limit must be a positive integer")
    limit = min(limit, 50)

    board = _goal_board(container)
    # ALWAYS pass user_id — GoalBoard.list(user_id=None) is a cross-tenant
    # read across every tenant's goals (see the plan's tenancy warning).
    goals = board.list(user_id=user_id, status=status, limit=limit)
    return _text_result({"goals": [_goal_summary(g) for g in goals]})


async def _call_goal_show(
    arguments: Dict[str, Any], user_id: str, container: Any
) -> Dict[str, Any]:
    goal_id = arguments.get("goal_id")
    if not goal_id or not isinstance(goal_id, str):
        return _error_result("goal_id is required")

    board = _goal_board(container)
    # GoalBoard.get() is cross-tenant-raw at the storage layer — the tenant
    # check happens HERE, post-fetch. A missing id and another tenant's id
    # produce the exact same "not found" (no existence oracle).
    goal = board.get(goal_id)
    if goal is None or goal.user_id != user_id:
        return _error_result(f"goal '{goal_id}' not found")
    return _text_result(_goal_summary(goal))


async def _call_conversations(
    arguments: Dict[str, Any], user_id: str, container: Any
) -> Dict[str, Any]:
    limit = arguments.get("limit", 30)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return _error_result("limit must be an integer")
    if limit < 1:
        return _error_result("limit must be a positive integer")
    limit = min(limit, 100)

    if container is None:
        raise _ToolUnavailable(
            "conversation store unavailable (server not fully initialized)"
        )
    store = container.get_service("conversation_store")
    if store is None:
        # No messaging surface has bootstrapped the store on this deploy —
        # an honest empty result, not an error.
        return _text_result({"conversations": ""})
    text = store.format_list(user_id, limit=limit)
    return _text_result({"conversations": text})


async def _call_pending_approvals(
    arguments: Dict[str, Any], user_id: str, container: Any
) -> Dict[str, Any]:
    from tools.controller.approval_queue import list_pending_tool_approvals

    board = _goal_board(container)
    # `board.asks(user_id=...)` (called inside list_pending_tool_approvals)
    # is tenant-scoped by construction — same board, same tenancy contract
    # as rob_goals_list.
    pending = list_pending_tool_approvals(board, user_id)
    return _text_result({"pending_approvals": pending})


_TOOL_DISPATCH = {
    "rob_usage_summary": _call_usage_summary,
    "rob_goals_list": _call_goals_list,
    "rob_goal_show": _call_goal_show,
    "rob_conversations": _call_conversations,
    "rob_pending_approvals": _call_pending_approvals,
}


async def _tools_call(
    params: Dict[str, Any], user_id: str, container: Any
) -> Dict[str, Any]:
    params = params or {}
    # Local fail-closed tenancy guard: get_user_permissive can yield None for a
    # validly-signed JWT missing a user_id claim (jwt_middleware passes the
    # claim through with no fallback), and GoalBoard.list(user_id=None) is a
    # cross-tenant read. This surface enforces tenancy itself rather than
    # trusting that upstream invariant. Exactly None — "" scopes to an empty
    # bucket everywhere (and usage_summary's ""->zeros contract is shipped).
    if user_id is None:
        return _error_result("not authenticated")
    name = params.get("name")
    fn = _TOOL_DISPATCH.get(name)
    if fn is None:
        raise MCPUnknownTool(name)
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error_result("arguments must be an object")
    try:
        return await fn(arguments, user_id, container)
    except _ToolUnavailable as e:
        return _error_result(str(e))


async def handle(
    method: str,
    params: Optional[Dict[str, Any]],
    user_id: str,
    container: Any = None,
) -> Optional[Dict[str, Any]]:
    """Dispatch one JSON-RPC method.

    Returns the JSON-RPC ``result`` payload, or ``None`` for a method whose
    JSON-RPC semantics is a notification (no response body expected).

    Raises :class:`MCPMethodNotFound` for anything not in the ladder.
    ``tools/call`` may additionally raise :class:`MCPUnknownTool` (unknown
    tool name — the router maps that to JSON-RPC INVALID_PARAMS).

    ``user_id``/``container`` are unused by ``initialize``/``tools/list``
    (no tenant data there) and threaded through to the ``tools/call``
    dispatch, which needs both for every one of the five tools.
    """
    params = params or {}
    if method == "initialize":
        return _initialize(params)
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _tools_list(params)
    if method == "tools/call":
        return await _tools_call(params, user_id, container)
    raise MCPMethodNotFound(method)
