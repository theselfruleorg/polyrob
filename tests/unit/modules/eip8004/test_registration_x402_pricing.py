"""Task 15 (Phase 4), deliverable 4: the ERC-8004 registration file must
advertise the agent's x402 pricing endpoint, so a machine payer discovering
the agent via 8004 Identity gets the A2A card AND knows it's payable.

This was ALREADY implemented (`build_registration_file` appends an `x402`
Endpoint pointing at `/api/x402/pricing` when `X402_ENABLED` is true) — this
test locks in that behavior rather than re-adding it.
"""
from modules.eip8004.registration import build_registration_file


def test_x402_pricing_endpoint_present_when_x402_enabled(monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "true")
    reg = build_registration_file("https://example.test")
    x402_endpoints = [e for e in reg.endpoints if e.name == "x402"]
    assert len(x402_endpoints) == 1
    assert x402_endpoints[0].endpoint == "https://example.test/api/x402/pricing"


def test_x402_pricing_endpoint_absent_when_x402_disabled(monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "false")
    reg = build_registration_file("https://example.test")
    assert not [e for e in reg.endpoints if e.name == "x402"]
