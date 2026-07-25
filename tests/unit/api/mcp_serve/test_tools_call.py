"""T2.3 Task 2 — the five ``tools/call`` bodies + tenant isolation.

Drives ``api.mcp_serve.handlers.handle()`` directly (async), the same
"exercise the handler, skip the HTTP plumbing" style
``tests/unit/api/test_a2a_ownership.py`` uses for A2A. Real ``GoalBoard`` on
``tmp_path`` (the repo norm — see ``tests/unit/tools/test_goal_tool_objectives.py``),
a fake ``conversation_store`` container service, and direct calls into
``usage_rollup``'s fail-open zeros path (no DI container needed).
"""
import asyncio

import pytest

from api.mcp_serve.handlers import MCPUnknownTool, handle
from agents.task.goals.board import GoalBoard


def _run(coro):
    return asyncio.run(coro)


def _call(name, arguments, user_id, container=None):
    return _run(
        handle(
            method="tools/call",
            params={"name": name, "arguments": arguments},
            user_id=user_id,
            container=container,
        )
    )


class _FakeConfig:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _FakeContainer:
    def __init__(self, data_dir=None, services=None):
        self.config = _FakeConfig(data_dir)
        self._services = services or {}

    def get_service(self, name):
        return self._services.get(name)


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


@pytest.fixture
def container(tmp_path):
    """A container whose `config.data_dir` points `_goal_board()` at the same
    tmp_path GoalBoard the `board` fixture constructs (both resolve to
    `<tmp_path>/goals.db` via goals_db_path)."""
    return _FakeContainer(data_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# rob_usage_summary
# ---------------------------------------------------------------------------

def test_usage_summary_happy_path_shape():
    result = _call("rob_usage_summary", {}, "u1")
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert '"user_id": "u1"' in result["content"][0]["text"]


def test_none_user_id_is_refused_before_any_tool_body():
    """Tenancy guard: get_user_permissive can yield None for a validly-signed
    JWT missing a user_id claim, and GoalBoard.list(user_id=None) is a
    cross-tenant read — a None principal must be refused at the dispatch
    choke point, never reach a tool body. ("" stays allowed: it scopes to an
    empty bucket everywhere, and usage_summary's ""->zeros contract is shipped.)"""
    for tool in ("rob_goals_list", "rob_usage_summary", "rob_goal_show",
                 "rob_conversations", "rob_pending_approvals"):
        result = _call(tool, {}, None)
        assert result["isError"] is True, f"{tool} accepted user_id=None"
        assert "not authenticated" in result["content"][0]["text"]


def test_usage_summary_anonymous_user_gets_zeros():
    # usage_rollup's own fail-open contract: a falsy user_id -> the all-zero
    # rollup, deterministically, with no db/container involved at all.
    result = _call("rob_usage_summary", {}, "")
    assert result["isError"] is False
    assert '"api_cost_usd": 0.0' in result["content"][0]["text"]
    assert '"calls": 0' in result["content"][0]["text"]


def test_usage_summary_bad_session_id_type_is_error():
    result = _call("rob_usage_summary", {"session_id": 123}, "u1")
    assert result["isError"] is True
    assert "session_id" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# rob_goals_list
# ---------------------------------------------------------------------------

def test_goals_list_happy_path_returns_only_callers_goals(board, container):
    board.create(user_id="u1", title="u1 goal A", force=True)
    board.create(user_id="u1", title="u1 goal B", force=True)
    board.create(user_id="u2", title="u2 secret goal", force=True)

    result = _call("rob_goals_list", {}, "u1", container)
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "u1 goal A" in text and "u1 goal B" in text
    assert "u2 secret goal" not in text


def test_goals_list_never_leaks_other_tenant_even_with_status_filter(board, container):
    board.create(user_id="u2", title="u2 only goal", force=True)
    result = _call("rob_goals_list", {"status": "ready"}, "u1", container)
    assert result["isError"] is False
    assert "u2 only goal" not in result["content"][0]["text"]


def test_goals_list_bad_limit_type_is_error(board, container):
    result = _call("rob_goals_list", {"limit": "lots"}, "u1", container)
    assert result["isError"] is True
    assert "limit" in result["content"][0]["text"]


def test_goals_list_zero_limit_is_error(board, container):
    result = _call("rob_goals_list", {"limit": 0}, "u1", container)
    assert result["isError"] is True


def test_goals_list_no_container_is_error(board):
    result = _call("rob_goals_list", {}, "u1", container=None)
    assert result["isError"] is True
    assert "unavailable" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# rob_goal_show
# ---------------------------------------------------------------------------

def test_goal_show_owner_can_see_own_goal(board, container):
    goal = board.create(user_id="u1", title="my goal")
    result = _call("rob_goal_show", {"goal_id": goal.id}, "u1", container)
    assert result["isError"] is False
    assert goal.id in result["content"][0]["text"]
    assert "my goal" in result["content"][0]["text"]


def test_goal_show_cross_tenant_gets_not_found_never_forbidden(board, container):
    victim_goal = board.create(user_id="u2", title="u2 private goal")
    result = _call("rob_goal_show", {"goal_id": victim_goal.id}, "u1", container)
    assert result["isError"] is True
    msg = result["content"][0]["text"].lower()
    assert "not found" in msg
    assert "forbidden" not in msg and "denied" not in msg
    # The private title must never leak into the error content either.
    assert "private" not in msg


def test_goal_show_missing_id_is_the_same_not_found_shape_as_cross_tenant(board, container):
    """No existence oracle: a genuinely-nonexistent id and another tenant's
    real id must be indistinguishable to the caller."""
    victim_goal = board.create(user_id="u2", title="u2 private goal")
    cross_tenant = _call("rob_goal_show", {"goal_id": victim_goal.id}, "u1", container)
    missing = _call("rob_goal_show", {"goal_id": "does-not-exist"}, "u1", container)
    assert cross_tenant["isError"] is True and missing["isError"] is True
    assert "not found" in cross_tenant["content"][0]["text"].lower()
    assert "not found" in missing["content"][0]["text"].lower()


def test_goal_show_missing_goal_id_param_is_error(board, container):
    result = _call("rob_goal_show", {}, "u1", container)
    assert result["isError"] is True
    assert "goal_id" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# rob_conversations
# ---------------------------------------------------------------------------

class _FakeConversationStore:
    def __init__(self):
        self.calls = []

    def format_list(self, user_id, limit=30):
        self.calls.append((user_id, limit))
        return f"conversations-for-{user_id}"


def test_conversations_happy_path_scopes_to_caller():
    store = _FakeConversationStore()
    container = _FakeContainer(services={"conversation_store": store})
    result = _call("rob_conversations", {"limit": 5}, "u1", container)
    assert result["isError"] is False
    assert "conversations-for-u1" in result["content"][0]["text"]
    assert store.calls == [("u1", 5)]


def test_conversations_never_asked_for_another_tenant():
    store = _FakeConversationStore()
    container = _FakeContainer(services={"conversation_store": store})
    _call("rob_conversations", {}, "u2", container)
    assert all(call[0] == "u2" for call in store.calls)


def test_conversations_no_store_registered_is_empty_not_error():
    container = _FakeContainer(services={})
    result = _call("rob_conversations", {}, "u1", container)
    assert result["isError"] is False
    assert result["content"][0]["text"] == '{"conversations": ""}'


def test_conversations_no_container_is_error():
    result = _call("rob_conversations", {}, "u1", container=None)
    assert result["isError"] is True
    assert "unavailable" in result["content"][0]["text"]


def test_conversations_bad_limit_is_error():
    store = _FakeConversationStore()
    container = _FakeContainer(services={"conversation_store": store})
    result = _call("rob_conversations", {"limit": -1}, "u1", container)
    assert result["isError"] is True
    assert store.calls == []


def test_conversations_limit_clamped_to_100():
    """(T2.3 review) an oversized limit must never reach the store
    unclamped — mirrors rob_goals_list's `min(limit, 50)` pattern."""
    store = _FakeConversationStore()
    container = _FakeContainer(services={"conversation_store": store})
    result = _call("rob_conversations", {"limit": 100000}, "u1", container)
    assert result["isError"] is False
    assert store.calls == [("u1", 100)]


# ---------------------------------------------------------------------------
# rob_pending_approvals
# ---------------------------------------------------------------------------

def test_pending_approvals_tenant_scoped(board, container):
    board.create_ask(
        user_id="u1", what="approve tool X for u1?",
        extra_payload={"ask_kind": "tool_approval"},
    )
    board.create_ask(
        user_id="u2", what="approve tool Y for u2?",
        extra_payload={"ask_kind": "tool_approval"},
    )
    result = _call("rob_pending_approvals", {}, "u1", container)
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "approve tool X for u1" in text
    assert "approve tool Y for u2" not in text


def test_pending_approvals_empty_for_tenant_with_none(board, container):
    board.create_ask(
        user_id="u2", what="approve tool Y for u2?",
        extra_payload={"ask_kind": "tool_approval"},
    )
    result = _call("rob_pending_approvals", {}, "u1", container)
    assert result["isError"] is False
    assert result["content"][0]["text"] == '{"pending_approvals": []}'


def test_pending_approvals_no_container_is_error(board):
    result = _call("rob_pending_approvals", {}, "u1", container=None)
    assert result["isError"] is True
    assert "unavailable" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Unknown tool / malformed params (dispatch-level contract)
# ---------------------------------------------------------------------------

def test_unknown_tool_raises_mcp_unknown_tool():
    with pytest.raises(MCPUnknownTool):
        _run(
            handle(
                method="tools/call",
                params={"name": "rob_definitely_not_a_tool", "arguments": {}},
                user_id="u1",
                container=None,
            )
        )


def test_missing_tool_name_raises_mcp_unknown_tool():
    with pytest.raises(MCPUnknownTool):
        _run(
            handle(method="tools/call", params={"arguments": {}}, user_id="u1", container=None)
        )


def test_non_object_arguments_is_error():
    result = _run(
        handle(
            method="tools/call",
            params={"name": "rob_pending_approvals", "arguments": "not-an-object"},
            user_id="u1",
            container=None,
        )
    )
    assert result["isError"] is True
    assert "arguments" in result["content"][0]["text"]


def test_missing_arguments_defaults_to_empty_object(board, container):
    result = _run(
        handle(
            method="tools/call",
            params={"name": "rob_pending_approvals"},
            user_id="u1",
            container=container,
        )
    )
    assert result["isError"] is False
