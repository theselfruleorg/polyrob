import pytest
from unittest.mock import AsyncMock, MagicMock

from core.surfaces import room_policy as rp
from core.tool_capabilities import ids_with


def test_default_room_tools_never_intersect_money_exec_delegate():
    forbidden = ids_with("money") | ids_with("exec") | ids_with("delegate_blocked")
    assert not (set(rp.DEFAULT_ROOM_TOOLS) & forbidden)


def test_env_override_drops_forbidden_ids(monkeypatch):
    monkeypatch.setenv("GROUP_TURN_TOOLS", "task,web_fetch,defi_trade,shell,goal")
    assert rp.room_tool_ids() == ["task", "web_fetch"]


def test_env_override_empty_string_is_an_empty_toolset_not_the_default(monkeypatch, caplog):
    """Fix round 1 (review Important #1): a deliberate lockdown (GROUP_TURN_TOOLS="")
    must resolve to [] -- NEVER floored back to DEFAULT_ROOM_TOOLS -- and must log a
    WARNING so an operator notices the room agent can now only chat."""
    monkeypatch.setenv("GROUP_TURN_TOOLS", "")
    assert rp.room_tool_ids() == []
    assert any("room toolset is empty" in r.getMessage() for r in caplog.records)


def test_env_override_all_forbidden_is_also_an_empty_toolset(monkeypatch, caplog):
    monkeypatch.setenv("GROUP_TURN_TOOLS", "defi_trade,shell")
    assert rp.room_tool_ids() == []
    assert any("room toolset is empty" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_create_session_empty_tool_ids_override_is_authoritative(tmp_path, monkeypatch):
    """Fix round 1 (review Important #1): agents/task_agent_lite.py's
    `tool_ids_override or session_request.tools` treated an explicit `[]` (a locked-down
    room toolset) as falsy and silently widened to session_request.tools (e.g.
    `default_session_tools()`, which includes `filesystem` -- read/write on the session
    workspace). An explicit override, including an empty one, must be authoritative."""
    from agents.task.agent.session import SessionManager
    from agents.task_agent_lite import TaskAgent
    from core.config import BotConfig
    from core.container import DependencyContainer

    config = BotConfig()
    container = DependencyContainer.get_instance(config)
    agent = TaskAgent(config=config, container=container)
    agent.session_manager = SessionManager(base_dir=str(tmp_path))
    agent.task_available = True
    agent._initialized = True

    mock_orchestrator = MagicMock()
    mock_orchestrator.initialize = AsyncMock()

    monkeypatch.setattr(
        "agents.task.agent.orchestrator.SessionOrchestrator",
        MagicMock(return_value=mock_orchestrator),
    )
    monkeypatch.setattr(agent, "register_orchestrator", MagicMock())

    await agent.create_session(
        "u1", request={"task": "t", "tools": ["filesystem"]},
        tool_ids=[], skip_credit_check=True,
    )

    mock_orchestrator.initialize.assert_awaited_once()
    _, kwargs = mock_orchestrator.initialize.await_args
    assert kwargs["tool_ids"] == []


def test_denied_actions_cover_deferred_execution_and_recall():
    for name in ("goal_create", "cronjob_schedule", "skill_manage", "self_context_manage",
                 "preferences", "owner_doc_manage", "load_tool", "session_search",
                 "memory_search", "contact_history", "recent_activity", "agent_status",
                 "insights", "usage_summary", "message", "x402_invoice_x402_request",
                 "defi_data_portfolio", "defi_data_positions", "defi_data_reconcile",
                 "delegate_task", "autonomy_control"):
        assert rp.is_room_denied_call(name, None), name


def test_speech_and_reads_are_allowed():
    for name in ("send_message", "done", "load_skill", "web_fetch_fetch", "defi_data_token_info"):
        assert not rp.is_room_denied_call(name, None), name


def test_hook_denies_only_in_public_sessions():
    hook = rp.make_room_gate_hook(lambda: True, resolve_tool=lambda n: None)
    assert hook("goal_create", {}, None)
    assert hook("browser_click", {}, None)          # high-impact prefix
    assert hook("send_message", {}, None) is None
    hook_private = rp.make_room_gate_hook(lambda: False, resolve_tool=lambda n: None)
    assert hook_private("goal_create", {}, None) is None


def test_hook_is_fail_closed():
    def boom():
        raise RuntimeError("probe")
    hook = rp.make_room_gate_hook(boom, resolve_tool=lambda n: None)
    assert hook("goal_create", {}, None)


# ---------------------------------------------------------------------------
# 044 spec ratchet 4 — the money check, and the prefix it must NOT over-match
# ---------------------------------------------------------------------------

def test_every_money_tool_is_denied_by_tool_id():
    from core.tool_capabilities import ids_with
    for tool_id in ids_with("money"):
        assert rp.is_room_denied_call(f"{tool_id}_anything", tool_id), tool_id


def test_a_money_verb_is_denied_by_name_alone():
    """`hyperliquid` and `polymarket` carry `money` but NOT `high_impact`, so the
    pre-existing high-impact check let `hyperliquid_place_order` through — and a
    controller that cannot resolve the owning tool passes tool_id=None."""
    for name in ("hyperliquid_place_order", "polymarket_buy", "defi_trade_swap",
                 "x402_pay_pay", "launchpad_launch", "dapp_browser_connect"):
        assert rp.is_room_denied_call(name, None), name


def test_the_namespace_fallback_does_not_over_match_a_sibling_prefix():
    """`polymarket_data` (read-only market data) and `polymarket` (money) share a
    prefix. A plain `startswith("polymarket_")` would deny every read-only quote
    as a money verb — the fallback matches the LONGEST tool id instead."""
    assert not rp.is_room_denied_call("polymarket_data_price", None)
    assert not rp.is_room_denied_call("polymarket_data_price", "polymarket_data")
    assert rp.is_room_denied_call("polymarket_data_price", "polymarket"), (
        "a RESOLVED money tool_id still wins over the name")


def test_the_knowledge_family_is_denied_by_prefix():
    for name in ("kb_search", "kb_ingest", "kb_list", "kb_remove", "kb_future_verb"):
        assert rp.is_room_denied_call(name, None), name
    assert rp.is_room_denied_call("anything", "knowledge")
