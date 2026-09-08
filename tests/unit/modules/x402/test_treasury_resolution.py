"""W1.1 (x402 self-contained rail, 2026-08-21): treasury auto-wire from the
agent wallet. `resolve_treasury_address()` is the ONE pay_to resolver:
explicit `X402_PAYMENT_RECIPIENT` always wins; when it is empty and
`X402_TREASURY_FROM_WALLET` (default ON) and the agent wallet is enabled,
the wallet's treasury-venue address fills in — read from memory at call
time, never written to any env file. Fail-open to '' on any wallet fault."""
import logging

import pytest

from modules.x402 import x402_integration
from modules.x402.x402_integration import get_x402_config, resolve_treasury_address

WALLET_ADDR = "0xWa11etTreasury00000000000000000000000001"
ENV_ADDR = "0xEnvRecipient0000000000000000000000000002"


class _FakeWallet:
    address = WALLET_ADDR


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("X402_PAYMENT_RECIPIENT", raising=False)
    monkeypatch.delenv("X402_TREASURY_FROM_WALLET", raising=False)


def _wire_wallet(monkeypatch, wallet):
    import core.wallet.factory as factory
    monkeypatch.setattr(factory, "get_agent_wallet", lambda: wallet)


def test_explicit_env_always_wins(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", ENV_ADDR)
    _wire_wallet(monkeypatch, _FakeWallet())
    assert resolve_treasury_address() == ENV_ADDR


def test_wallet_fills_empty_env(monkeypatch):
    _wire_wallet(monkeypatch, _FakeWallet())
    assert resolve_treasury_address() == WALLET_ADDR


def test_flag_off_restores_legacy_empty(monkeypatch):
    monkeypatch.setenv("X402_TREASURY_FROM_WALLET", "false")
    _wire_wallet(monkeypatch, _FakeWallet())
    assert resolve_treasury_address() == ""


def test_wallet_disabled_stays_empty(monkeypatch):
    _wire_wallet(monkeypatch, None)  # AGENT_WALLET_ENABLED off -> factory None
    assert resolve_treasury_address() == ""


def test_wallet_fault_fails_open_to_empty(monkeypatch):
    import core.wallet.factory as factory

    def boom():
        raise RuntimeError("wallet exploded")

    monkeypatch.setattr(factory, "get_agent_wallet", boom)
    assert resolve_treasury_address() == ""


def test_mismatch_warns_once_and_env_wins(monkeypatch, caplog):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", ENV_ADDR)
    _wire_wallet(monkeypatch, _FakeWallet())
    monkeypatch.setattr(x402_integration, "_TREASURY_MISMATCH_WARNED", False)
    with caplog.at_level(logging.WARNING):
        assert resolve_treasury_address() == ENV_ADDR
        assert resolve_treasury_address() == ENV_ADDR
    warns = [r for r in caplog.records if "differs" in r.getMessage()]
    assert len(warns) == 1  # one-time, not per-call


def test_get_x402_config_pay_to_uses_resolver(monkeypatch):
    _wire_wallet(monkeypatch, _FakeWallet())
    assert get_x402_config()["pay_to"] == WALLET_ADDR
