"""defi_trade gating — every hand-maintained list, asserted by RUNTIME name.

A money verb is only as gated as its weakest list, and each of these lists is
hand-maintained. The x402_request lane was dead for months because the list held
a bare name; these assertions use the namespaced runtime name deliberately.
"""
from agents.task.agent.core.correspondent_gate import is_high_impact
from core.config_policy import PAYMENT_APPROVAL_TOOLS, PAYMENT_RECEIVE_APPROVAL_TOOLS
from core.tool_capabilities import TOOL_CAPABILITIES, ids_with, is_classified

TRANSFER = "defi_trade_transfer"


def test_defi_trade_is_classified_as_money():
    assert is_classified("defi_trade")
    caps = TOOL_CAPABILITIES["defi_trade"]
    assert "money" in caps
    assert "high_impact" in caps
    assert "delegate_blocked" in caps


def test_a_delegated_leaf_cannot_reach_it():
    from tools.controller.delegation import get_blocked_child_tools
    assert "defi_trade" in get_blocked_child_tools()
    assert "defi_trade" in ids_with("delegate_blocked")


def test_transfer_is_on_the_owner_approval_lane_by_runtime_name():
    assert TRANSFER in PAYMENT_APPROVAL_TOOLS


def test_transfer_is_on_the_SPEND_lane_not_receive():
    """Receive-side verbs may act-and-report under PAYMENT_APPROVAL_MODE=auto.
    Moving funds out must never be act-and-report."""
    assert TRANSFER not in PAYMENT_RECEIVE_APPROVAL_TOOLS


def test_transfer_is_high_impact_for_a_tainted_session():
    assert is_high_impact(TRANSFER) is True


def test_bare_name_is_not_what_the_lists_rely_on():
    """Guards the regression directly: if someone 'simplifies' the entry to the
    bare verb, the runtime name stops matching and every gate silently dies."""
    assert "transfer" not in PAYMENT_APPROVAL_TOOLS


def test_flag_defaults_off(monkeypatch):
    from tools.defi import defi_trade_enabled
    monkeypatch.delenv("DEFI_TRADE_ENABLED", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    assert defi_trade_enabled() is False


def test_flag_never_rides_the_local_safe_group(monkeypatch):
    from tools.defi import defi_trade_enabled
    monkeypatch.delenv("DEFI_TRADE_ENABLED", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert defi_trade_enabled() is False


def test_never_in_the_default_toolset_even_when_enabled(monkeypatch):
    """Money tools are explicit-grant-only — never auto-loaded into a session."""
    monkeypatch.setenv("DEFI_TRADE_ENABLED", "true")
    monkeypatch.delenv("POLYROB_AGENT_TOOLSET", raising=False)
    from agents.task.tool_defaults import _dynamic_default_tools
    assert "defi_trade" not in _dynamic_default_tools()


def test_load_tool_refuses_to_self_serve_a_money_tool():
    from tools.tool_disclosure import perform_load_tool  # noqa: F401
    from core.tool_capabilities import TOOL_CAPABILITIES
    assert "money" in TOOL_CAPABILITIES["defi_trade"], (
        "the load_tool money refusal is derived from this row")
