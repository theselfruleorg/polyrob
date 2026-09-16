"""The AMBIENT session toolset never carries a money-SPEND tool (042 audit).

A session that asked for NO tools gets `tool_defaults.default_session_tools()` /
`server_default_tools()`. Those read the BARE `AUTONOMOUS_MODE_TOOLS`, not
`autonomous_mode_tools()` — deliberately, because the accessor folds in the
`DEFI_AGENT_AUTONOMY` grant, and a spend verb belongs to a session an owner
deliberately armed rather than to the default of one that asked for nothing.

An alignment audit read that as drift, which is fair: the invariant lived in one
loose docstring sentence and nothing tested it. Now it is tested, in both
directions — so neither a "fix" that widens it nor a drift that narrows the
armed path can pass green.
"""
import pytest

from agents.task import tool_defaults
from core.tool_capabilities import ids_with

#: `x402_invoice` is money-classified but RECEIVE-side — it bills, it does not
#: spend. Everything else with the `money` capability moves value OUT.
RECEIVE_ONLY = {"x402_invoice"}
SPEND_TOOLS = set(ids_with("money")) - RECEIVE_ONLY


@pytest.fixture()
def everything_armed(monkeypatch):
    for flag in ("AUTONOMY_MODE",):
        monkeypatch.setenv(flag, "autonomous")
    for flag in ("POLYROB_LOCAL", "DEFI_AGENT_AUTONOMY", "DEFI_TRADE_ENABLED",
                 "LAUNCHPAD_ENABLED", "DAPP_BROWSER_ENABLED",
                 "DEFI_DEPLOY_ENABLED", "DEFI_CALL_ENABLED"):
        monkeypatch.setenv(flag, "true")


def test_there_are_spend_tools_to_check():
    """Anti-vacuous: if the derivation ever returns nothing, every assertion
    below passes while checking nothing."""
    assert {"defi_trade", "launchpad", "dapp_browser"} <= SPEND_TOOLS


@pytest.mark.parametrize("fn", ["default_session_tools", "server_default_tools"])
def test_the_ambient_toolset_never_carries_a_spend_tool(fn, everything_armed):
    got = set(getattr(tool_defaults, fn)())
    leak = got & SPEND_TOOLS
    assert not leak, (
        f"{fn}() would hand a session that asked for NO tools the spend verbs "
        f"{sorted(leak)}. These read the BARE AUTONOMOUS_MODE_TOOLS on purpose; "
        f"switching to autonomous_mode_tools() is what does this.")


@pytest.mark.parametrize("fn", ["default_session_tools", "server_default_tools"])
def test_the_ambient_toolset_is_not_empty(fn, everything_armed):
    """The other direction: a boundary that returns nothing is not a boundary,
    it is a broken default."""
    assert getattr(tool_defaults, fn)()


def test_the_ARMED_paths_DO_get_them(everything_armed):
    """The grant is not dead — it reaches the consumers it was written for.
    This is the half the flag exists to deliver, and the half that was broken
    on 2026-09-12 when the owner's own chat held neither tool."""
    from agents.task.constants import autonomous_mode_tools
    granted = set(autonomous_mode_tools())
    assert {"defi_trade", "launchpad", "dapp_browser"} <= granted


def test_the_two_are_genuinely_different(everything_armed):
    """If the accessor and the constant ever converge, one of the two tests
    above is passing for the wrong reason."""
    from agents.task.constants import AUTONOMOUS_MODE_TOOLS, autonomous_mode_tools
    assert set(autonomous_mode_tools()) - set(AUTONOMOUS_MODE_TOOLS)
