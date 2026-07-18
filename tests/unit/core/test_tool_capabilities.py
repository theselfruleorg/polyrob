"""WS-2: the per-tool capability table derives the tool-id gate sets exactly.

Byte-for-byte parity with the pre-derivation literals, full coverage of the registrable
vocabulary, and the deliberate polarities (delegable-but-high-impact comms tools;
readable-while-tainted trading venues; x402_pay fully blocked).
"""
from core.tool_capabilities import (
    KNOWN_CAPABILITIES,
    TOOL_CAPABILITIES,
    ids_with,
    is_classified,
)


def test_money_derivation_exact():
    assert ids_with("money") == frozenset(
        {"x402_pay", "x402_invoice", "hyperliquid", "polymarket"})


def test_delegate_blocked_derivation_exact():
    assert ids_with("delegate_blocked") == frozenset({
        "code_execution", "coding", "cronjob", "x402_pay", "x402_invoice",
        "hyperliquid", "polymarket", "git", "github", "process", "tool_manage",
        "mcp", "shell", "self_env", "hf_deploy",
    })


def test_high_impact_derivation_exact():
    assert ids_with("high_impact") == frozenset({
        "code_execution", "coding", "cronjob", "goal", "x402_pay", "email",
        "twitter", "browser", "web_fetch", "git", "github", "mcp", "process",
        "tool_manage", "shell", "self_env", "x402_invoice", "anysite",
        "perplexity", "hf_deploy",
    })


def test_gate_modules_actually_derive_from_the_table():
    """The named sets in the gate modules must BE the derivations (identity of value),
    so table edits propagate and the hand-lists cannot silently drift again."""
    from agents.task.runtime.metering_gate import MONEY_TOOLS
    from tools.controller.delegation import DELEGATE_BLOCKED_TOOLS
    from agents.task.agent.core.correspondent_gate import HIGH_IMPACT_TOOL_IDS

    assert MONEY_TOOLS == ids_with("money")
    assert DELEGATE_BLOCKED_TOOLS == ids_with("delegate_blocked")
    assert HIGH_IMPACT_TOOL_IDS == ids_with("high_impact")


def test_every_valid_tool_id_is_classified():
    """A registrable tool without a capability row is the drift this table exists to
    prevent. An explicit empty frozenset() IS a classification."""
    from agents.task.agent.skill_manager import VALID_TOOL_IDS

    unclassified = set(VALID_TOOL_IDS) - set(TOOL_CAPABILITIES)
    assert not unclassified, f"classify these in core/tool_capabilities.py: {sorted(unclassified)}"


def test_no_unknown_capability_tokens():
    for tool, caps in TOOL_CAPABILITIES.items():
        unknown = caps - KNOWN_CAPABILITIES
        assert not unknown, f"{tool}: unknown capability token(s) {sorted(unknown)}"


def test_polarities_preserved():
    # email/twitter/browser: delegable-but-high-impact.
    for t in ("email", "twitter", "browser"):
        assert "high_impact" in TOOL_CAPABILITIES[t]
        assert "delegate_blocked" not in TOOL_CAPABILITIES[t]
    # trading venues: readable while tainted (NOT high_impact as a tool_id), money.
    for t in ("hyperliquid", "polymarket"):
        caps = TOOL_CAPABILITIES[t]
        assert {"money", "readable_while_tainted"} <= caps
        assert "high_impact" not in caps
    # x402_pay: fully blocked while tainted (its only verb is auto-pay).
    assert "high_impact" in TOOL_CAPABILITIES["x402_pay"]
    # `task` is the TODO tool, never blocked anywhere.
    assert TOOL_CAPABILITIES["task"] == frozenset()


def test_catalog_risk_tiers_derive_exactly():
    """The catalog risk tiers (folded from core/tool_catalog.py's hand-sets) must
    derive to the SAME memberships — independent literals, not self-comparison."""
    from core.tool_capabilities import high_risk_tool_ids, medium_risk_tool_ids

    assert high_risk_tool_ids() == frozenset(
        {"twitter", "email", "polymarket", "hyperliquid"})
    assert medium_risk_tool_ids() == frozenset(
        {"mcp", "anysite", "browser_manager", "perplexity"})


def test_catalog_back_compat_names_are_the_derivations():
    from core.tool_catalog import _HIGH_RISK_TOOLS, _MEDIUM_RISK_TOOLS, _PERMISSIONS
    from core.tool_capabilities import (
        TOOL_PERMISSIONS, high_risk_tool_ids, medium_risk_tool_ids,
    )

    assert _HIGH_RISK_TOOLS == high_risk_tool_ids()
    assert _MEDIUM_RISK_TOOLS == medium_risk_tool_ids()
    assert _PERMISSIONS == {k: list(v) for k, v in TOOL_PERMISSIONS.items()}


def test_every_permissions_key_is_classified():
    """A permissions row for a tool with no capability row would be the same drift
    the table exists to prevent (via the browser_manager -> browser alias)."""
    from core.tool_capabilities import CATALOG_ALIASES, TOOL_PERMISSIONS

    unclassified = {
        t for t in TOOL_PERMISSIONS
        if not is_classified(CATALOG_ALIASES.get(t, t))
    }
    assert not unclassified, f"classify these in TOOL_CAPABILITIES: {sorted(unclassified)}"


def test_registration_guard_refuses_unclassified_tool():
    """The actual WS-2 win: a NEW optional tool with no capability row fails loudly at
    registration instead of silently skipping every gate."""
    import pytest
    from tools.base_tool import BaseTool
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool

    class _Phantom(BaseTool):  # pragma: no cover - never initialized
        pass

    desc = ToolDescriptor(
        name="phantom_unclassified_tool",
        description="test-only",
        category=ToolCategory.INTEGRATION,
    )
    with pytest.raises(ValueError, match="capabilit"):
        register_optional_tool("phantom_unclassified_tool", _Phantom, desc,
                               lambda: False, force=True)
    assert not is_classified("phantom_unclassified_tool")
