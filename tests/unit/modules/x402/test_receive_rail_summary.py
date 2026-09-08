"""§5.2 (self-contained x402 rail, 2026-08-21): the AGENT's own view of its
receive rail.

Cancelling the historical "owner deploy package: mainnet-ready x402 endpoint"
goal does NOT stop it being re-filed — board dedup ignores cancelled rows. The
durable fix is that the agent can SEE, where it already looks for money state,
that (a) it can already get paid with no endpoint, and (b) an HTTP endpoint is
an owner-only step with a named runbook — never agent work.
"""
import pytest

from modules.x402.x402_integration import receive_rail_summary


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for var in ("X402_PAYMENT_RECIPIENT", "X402_TREASURY_FROM_WALLET",
                "X402_SETTLE_ONCHAIN_DETECT", "X402_ENABLED", "A2A_BASE_URL",
                "X402_DEFAULT_CHAIN", "AUTONOMY_MODE"):
        monkeypatch.delenv(var, raising=False)
    import core.wallet.factory as factory
    monkeypatch.setattr(factory, "get_agent_wallet", lambda: None)


def test_summary_names_treasury_and_chain(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTreasury1")
    out = receive_rail_summary()
    assert "0xTreasury1" in out
    assert "base" in out


def test_no_treasury_is_stated_as_the_blocker(monkeypatch):
    out = receive_rail_summary()
    assert "no treasury" in out.lower()


def test_endpoint_absent_is_owner_work_not_agent_work(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTreasury1")
    out = receive_rail_summary()
    # the two facts that stop a re-filed "build me an endpoint" goal
    assert "no HTTP endpoint" in out
    assert "not needed to get paid" in out
    assert "owner-only" in out
    assert "scripts/setup_x402_endpoint.sh" in out


def test_endpoint_live_is_reported_and_drops_the_runbook(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTreasury1")
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("A2A_BASE_URL", "https://agent.example.com")
    out = receive_rail_summary()
    assert "https://agent.example.com" in out
    assert "setup_x402_endpoint.sh" not in out


def test_detect_state_is_reported(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTreasury1")
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    assert "detect ON" in receive_rail_summary()
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "false")
    assert "detect OFF" in receive_rail_summary()


def test_summary_never_raises(monkeypatch):
    import modules.x402.x402_integration as xi

    def boom():
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(xi, "resolve_treasury_address", boom)
    assert receive_rail_summary() == ""
