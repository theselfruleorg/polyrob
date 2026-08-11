"""defi_data gating: classification, untrusted wrapping, taint scope, flag default.

The portfolio gate is asserted under its NAMESPACED runtime name. A bare name
silently never matches — that is not hypothetical, it is live today for
`x402_request` (audit 2026-08-07 P0-2), where PAYMENT_APPROVAL_TOOLS lists the
bare verb while the action registers as `x402_invoice_x402_request`, so the
owner-approval lane has never fired.
"""
from agents.task.agent.core.correspondent_gate import is_high_impact
from core.security.untrusted_wrap import is_untrusted_tool
from core.tool_capabilities import TOOL_CAPABILITIES, is_classified


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def test_defi_data_is_classified():
    assert is_classified("defi_data"), "register_optional_tool refuses an unclassified tool"
    assert TOOL_CAPABILITIES["defi_data"] == frozenset()


def test_defi_data_is_not_a_money_tool():
    from core.tool_capabilities import ids_with
    assert "defi_data" not in ids_with("money"), "this tier cannot move value"
    assert "defi_data" not in ids_with("exec")


# --------------------------------------------------------------------------
# Untrusted wrapping — token name/symbol are attacker-authored
# --------------------------------------------------------------------------

def test_defi_output_is_untrusted_wrapped():
    assert is_untrusted_tool("defi_data_token_info", "defi_data") is True
    assert is_untrusted_tool("defi_data_portfolio", "defi_data") is True


# --------------------------------------------------------------------------
# Correspondent taint — own holdings only
# --------------------------------------------------------------------------

def test_portfolio_is_gated_under_its_namespaced_runtime_name():
    assert is_high_impact("defi_data_portfolio") is True


def test_impersonal_reads_stay_available_while_tainted():
    for action in ("defi_data_token_info", "defi_data_price",
                   "defi_data_token_resolve", "defi_data_contract_read"):
        assert is_high_impact(action) is False, f"{action} should stay readable"


# --------------------------------------------------------------------------
# Flag: OFF by default and NOT in the POLYROB_LOCAL safe group
# --------------------------------------------------------------------------

def test_flag_defaults_off(monkeypatch):
    from tools.defi import defi_data_enabled
    monkeypatch.delenv("DEFI_DATA_ENABLED", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    assert defi_data_enabled() is False


def test_flag_does_not_ride_the_local_safe_group(monkeypatch):
    """Production runs POLYROB_LOCAL=1 with a live mainnet wallet — joining the
    safe group would auto-enable this there at the next deploy."""
    from tools.defi import defi_data_enabled
    monkeypatch.delenv("DEFI_DATA_ENABLED", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert defi_data_enabled() is False


def test_flag_honours_explicit_opt_in(monkeypatch):
    from tools.defi import defi_data_enabled
    monkeypatch.setenv("DEFI_DATA_ENABLED", "true")
    assert defi_data_enabled() is True


def test_register_is_a_noop_when_disabled(monkeypatch):
    from tools.defi import register_defi_data_tool
    monkeypatch.delenv("DEFI_DATA_ENABLED", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    assert register_defi_data_tool() is False


def test_register_when_forced_classifies_and_registers():
    """Force-registration succeeds — and is undone, because TOOL_DESCRIPTORS is
    process-global and leaking `defi_data` into it breaks
    tests/unit/core/test_tool_registration_parity.py later in the same session.
    """
    from tools.descriptors import TOOL_DESCRIPTORS
    from tools.defi import register_defi_data_tool

    had = "defi_data" in TOOL_DESCRIPTORS
    prior = TOOL_DESCRIPTORS.get("defi_data")
    try:
        assert register_defi_data_tool(force=True) is True
        assert "defi_data" in TOOL_DESCRIPTORS
    finally:
        if had:
            TOOL_DESCRIPTORS["defi_data"] = prior
        else:
            TOOL_DESCRIPTORS.pop("defi_data", None)


def test_not_in_default_toolset_when_disabled(monkeypatch):
    monkeypatch.delenv("DEFI_DATA_ENABLED", raising=False)
    monkeypatch.delenv("POLYROB_AGENT_TOOLSET", raising=False)
    from agents.task.tool_defaults import _dynamic_default_tools
    assert "defi_data" not in _dynamic_default_tools()


def test_in_default_toolset_when_enabled(monkeypatch):
    monkeypatch.setenv("DEFI_DATA_ENABLED", "true")
    monkeypatch.delenv("POLYROB_AGENT_TOOLSET", raising=False)
    from agents.task.tool_defaults import _dynamic_default_tools
    assert "defi_data" in _dynamic_default_tools(), (
        "registering a tool does not make it callable — it must join tool_ids")
