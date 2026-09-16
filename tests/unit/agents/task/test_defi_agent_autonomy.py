"""`DEFI_AGENT_AUTONOMY` — the on-chain rail in the autonomous grant (2026-09-12).

Owner directive: *"we should GIVE AGENT TOOLS TO DO EVERY DEFI ACTION WITHOUT
USER"*. The failure it fixes was total: with defi absent from the grant, the
OWNER'S OWN CHAT SESSION held neither defi tool, so asking his agent to bridge
returned "no bridge verb in the tool catalog" about a rail that was deployed and
working.

Default OFF: a money capability ships disarmed, and the unarmed set must stay
byte-identical to the historical one.
"""
import pytest

from agents.task import constants


def test_default_is_OFF_and_byte_identical(monkeypatch):
    monkeypatch.delenv("DEFI_AGENT_AUTONOMY", raising=False)
    assert constants.autonomous_mode_tools() == constants.AUTONOMOUS_MODE_TOOLS


def test_armed_adds_exactly_the_defi_rail(monkeypatch):
    """042 widened this to the launchpad and the injected dapp wallet, for the
    same reason the flag exists: an agent that cannot REACH the capability its
    owner is asking about teaches him that a shipped feature is broken.

    Both additions stay independently flag-gated (LAUNCHPAD_ENABLED /
    DAPP_BROWSER_ENABLED, default OFF) — pinned below — so this grant makes them
    reachable, never armed.
    """
    monkeypatch.setenv("DEFI_AGENT_AUTONOMY", "true")
    got = set(constants.autonomous_mode_tools())
    assert got - set(constants.AUTONOMOUS_MODE_TOOLS) == {
        "defi_data", "defi_trade", "launchpad", "dapp_browser"}


def test_reaching_a_money_tool_is_not_arming_it(monkeypatch):
    """Two keys, not one. With the grant armed and nothing else set, the two
    042 tools are in the toolset and still refuse every call."""
    monkeypatch.setenv("DEFI_AGENT_AUTONOMY", "true")
    monkeypatch.delenv("LAUNCHPAD_ENABLED", raising=False)
    monkeypatch.delenv("DAPP_BROWSER_ENABLED", raising=False)
    granted = set(constants.autonomous_mode_tools())
    assert {"launchpad", "dapp_browser"} <= granted

    from tools.dapp_browser.tool import dapp_browser_enabled
    from tools.launchpad.tool import launchpad_enabled
    assert launchpad_enabled() is False
    assert dapp_browser_enabled() is False


def test_arming_never_adds_payment_or_host_tools(monkeypatch):
    """This widens the on-chain TRADING rail, not arbitrary payment or host
    access. x402_pay/wallet/hyperliquid/polymarket and every host tool stay out
    even when armed."""
    monkeypatch.setenv("DEFI_AGENT_AUTONOMY", "true")
    forbidden = {"x402_pay", "wallet", "hyperliquid", "polymarket",
                 "code_execution", "shell", "self_env", "process"}
    assert forbidden.isdisjoint(set(constants.autonomous_mode_tools()))


def test_it_is_resolved_at_call_time_not_frozen(monkeypatch):
    """An operator must be able to arm or disarm it without a redeploy."""
    monkeypatch.delenv("DEFI_AGENT_AUTONOMY", raising=False)
    before = constants.autonomous_mode_tools()
    monkeypatch.setenv("DEFI_AGENT_AUTONOMY", "true")
    assert constants.autonomous_mode_tools() != before


@pytest.mark.parametrize("consumer", [
    "surfaces/telegram/interactive_tools.py",
    "agents/task/goals/dispatcher.py",
    "tools/goal_tools.py",
])
def test_every_consumer_reads_the_accessor_not_the_bare_constant(consumer):
    """A consumer still reading AUTONOMOUS_MODE_TOOLS directly silently ignores
    the flag — which is exactly how the owner's chat session ended up with no
    defi tool while the rail was live."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[4]
    src = (root / consumer).read_text()
    assert "autonomous_mode_tools" in src, f"{consumer} bypasses the accessor"
