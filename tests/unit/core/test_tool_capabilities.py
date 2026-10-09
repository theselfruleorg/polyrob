"""WS-2: the per-tool capability table derives the tool-id gate sets exactly.

Byte-for-byte parity with the pre-derivation literals, full coverage of the registrable
vocabulary, and the deliberate polarities (delegable-but-high-impact comms tools;
readable-while-tainted trading venues; x402_pay fully blocked).
"""
import pytest

from core.tool_capabilities import (
    KNOWN_CAPABILITIES,
    TOOL_CAPABILITIES,
    ids_with,
    is_classified,
)


def test_money_derivation_exact():
    assert ids_with("money") == frozenset(
        {"x402_pay", "x402_invoice", "hyperliquid", "polymarket", "defi_trade",
         "launchpad", "dapp_browser", "agent_nft"})


def test_delegate_blocked_derivation_exact():
    assert ids_with("delegate_blocked") == frozenset({
        "code_execution", "coding", "cronjob", "x402_pay", "x402_invoice",
        "hyperliquid", "polymarket", "git", "github", "process", "tool_manage",
        "mcp", "shell", "self_env", "hf_deploy", "defi_trade", "x_browser",
        "publish", "app_service", "launchpad", "dapp_browser", "worker_manage", "agent_nft", "twitter",
    })


def test_high_impact_derivation_exact():
    assert ids_with("high_impact") == frozenset({
        "code_execution", "coding", "cronjob", "goal", "x402_pay", "email",
        "twitter", "browser", "web_fetch", "git", "github", "mcp", "process",
        "tool_manage", "shell", "self_env", "x402_invoice", "anysite",
        "perplexity", "hf_deploy", "defi_trade", "x_browser", "publish",
        "app_service", "launchpad", "dapp_browser", "worker_manage", "agent_nft",
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
    # email/browser: delegable-but-high-impact. X requires an owner turn.
    for t in ("email", "browser"):
        assert "high_impact" in TOOL_CAPABILITIES[t]
        assert "delegate_blocked" not in TOOL_CAPABILITIES[t]
    assert {"high_impact", "delegate_blocked"} <= TOOL_CAPABILITIES["twitter"]
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
    derive to the SAME memberships — independent literals, not self-comparison.

    F44/A9 (2026-09-14): filling the 19-id TOOL_PERMISSIONS gap widened both
    sets — money/exec ids (code_execution/coding/shell/process/self_env/
    x402_pay/x402_invoice/defi_trade) carry an external-write permission and
    land HIGH; high_impact-only ids (goal/cronjob/git/github/hf_deploy/
    publish/app_service/tool_manage/web_fetch) carry none and land MEDIUM."""
    from core.tool_capabilities import high_risk_tool_ids, medium_risk_tool_ids

    assert high_risk_tool_ids() == frozenset(
        {"twitter", "email", "polymarket", "hyperliquid", "x_browser",
         "launchpad", "dapp_browser", "agent_nft",
         "code_execution", "coding", "shell", "process", "self_env",
         "x402_pay", "x402_invoice", "defi_trade"})
    assert medium_risk_tool_ids() == frozenset(
        {"mcp", "anysite", "browser_manager", "perplexity",
         "goal", "cronjob", "git", "github", "hf_deploy", "publish",
         "app_service", "tool_manage", "web_fetch", "worker_manage"})


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


def test_effect_tokens_are_known_capabilities():
    from core.tool_capabilities import EFFECT_CAPABILITIES, KNOWN_CAPABILITIES
    assert EFFECT_CAPABILITIES <= KNOWN_CAPABILITIES
    assert EFFECT_CAPABILITIES == frozenset({
        "writes_social", "writes_comms", "writes_public",
        "writes_money", "writes_code", "writes_self", "writes_network"})


def test_money_and_exec_tools_all_declare_an_effect():
    from core.tool_capabilities import TOOL_CAPABILITIES, lacks_effect_ceiling
    missing = sorted(t for t in TOOL_CAPABILITIES if lacks_effect_ceiling(t))
    assert missing == [], f"write-capable tools with no writes_* token: {missing}"
    assert ids_with("money") <= ids_with("writes_money")


def test_registration_guard_refuses_a_writer_with_no_effect(monkeypatch):
    """033: a classified row that can move money but names no effect ceiling is
    refused at registration, exactly like an unclassified one."""
    import pytest
    import core.tool_capabilities as tc
    from tools.base_tool import BaseTool
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool

    class _Phantom(BaseTool):  # pragma: no cover - never initialized
        pass

    monkeypatch.setitem(tc.TOOL_CAPABILITIES, "phantom_money_tool",
                        frozenset({"money", "high_impact"}))
    desc = ToolDescriptor(name="phantom_money_tool", description="test-only",
                          category=ToolCategory.INTEGRATION)
    with pytest.raises(ValueError, match="writes_"):
        register_optional_tool("phantom_money_tool", _Phantom, desc,
                               lambda: False, force=True)


# --- 067 P1: per-tool policy fields on the rows -----------------------------------

def test_rows_are_toolrows_and_still_plain_capability_sets():
    from core.tool_capabilities import ToolRow
    for tid, row in TOOL_CAPABILITIES.items():
        assert isinstance(row, ToolRow), tid
        assert frozenset(row) == row  # the set half is unchanged
    import copy
    import pickle
    row = TOOL_CAPABILITIES["x_browser"]
    for clone in (copy.deepcopy(row), pickle.loads(pickle.dumps(row))):
        assert clone == row and clone.fields() == row.fields()


def test_untrusted_namespaces_are_a_view_of_the_rows():
    from core.security.untrusted_wrap import UNTRUSTED_TOOL_NAMESPACES
    from core.tool_capabilities import ids_where
    assert UNTRUSTED_TOOL_NAMESPACES == ids_where("untrusted_output")


def test_cli_tables_are_views_of_the_rows():
    import importlib

    from core import bootstrap as bs
    from core.tool_capabilities import descriptor_id, ids_where, row_field
    assert bs._CLI_INCOMPATIBLE == set(ids_where("cli", "incompatible"))
    assert bs._CLI_STATIC_TOOLS == {descriptor_id(t) for t in ids_where("cli", "static")}
    services = {s for _m, _f, svcs in bs._CLI_OPTIONAL_REGISTRARS for s in svcs}
    assert services == set(ids_where("cli", "optional"))
    for module_path, fn_name, _svcs in bs._CLI_OPTIONAL_REGISTRARS:
        assert callable(getattr(importlib.import_module(module_path), fn_name))
    assert row_field("not-a-tool", "cli") == "none"


def test_toolrow_refuses_an_inconsistent_cli_field():
    from core.tool_capabilities import ToolRow
    with pytest.raises(ValueError):
        ToolRow(cli="optional")
    with pytest.raises(ValueError):
        ToolRow(cli="static", cli_registrar="tools.x:y")
    with pytest.raises(ValueError):
        ToolRow(cli="bogus")
