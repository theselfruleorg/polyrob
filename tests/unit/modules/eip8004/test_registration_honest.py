"""F3c: the registration file must not CLAIM on-chain identity it hasn't verified.

Default = local trust mode, no registrations[] block. Only when an operator
declares EIP8004_ONCHAIN_ENABLED do we assert the on-chain registration.
"""
import pytest

from modules.eip8004.registration import build_registration_file


def _set_identity(monkeypatch):
    monkeypatch.setenv("EIP8004_AGENT_ID", "42")
    monkeypatch.setenv("EIP8004_IDENTITY_REGISTRY", "0x" + "1" * 40)


def test_local_trust_mode_by_default(monkeypatch):
    _set_identity(monkeypatch)
    monkeypatch.delenv("EIP8004_ONCHAIN_ENABLED", raising=False)
    reg = build_registration_file("https://example.test")
    assert reg.trustMode == "local"
    # Do NOT advertise an unverified on-chain identity.
    assert reg.registrations == []


def test_onchain_trust_mode_when_declared(monkeypatch):
    _set_identity(monkeypatch)
    monkeypatch.setenv("EIP8004_ONCHAIN_ENABLED", "true")
    reg = build_registration_file("https://example.test")
    assert reg.trustMode == "onchain"
    assert len(reg.registrations) == 1
