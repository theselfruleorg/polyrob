"""Regression: POLYROB_LOCAL must NOT auto-own a forgeable network sender.

guard_inbound honored POLYROB_LOCAL unconditionally, so with pairing required an
arbitrary Telegram/email sender was granted 'owner' and the pairing gate was
fully bypassed. Local-owner must be scoped to trusted local surfaces only.
"""
import types

import pytest

from core.pairing import guard_inbound


@pytest.fixture
def container(tmp_path):
    cfg = types.SimpleNamespace(data_dir=str(tmp_path))
    return types.SimpleNamespace(config=cfg)


@pytest.fixture(autouse=True)
def _pairing_on(monkeypatch):
    monkeypatch.setenv("POLYROB_REQUIRE_PAIRING", "1")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    # No bound owner principal — only local-surface owners should pass.
    monkeypatch.delenv("POLYROB_OWNER_PRINCIPAL", raising=False)
    monkeypatch.delenv("BOT_OWNER_PRINCIPAL", raising=False)


@pytest.mark.parametrize("surface", ["telegram", "email", "whatsapp"])
def test_network_surface_stranger_denied_despite_local(container, surface):
    decision = guard_inbound(container, "stranger-123", surface_id=surface)
    assert decision is not None, f"{surface} sender must be DENIED, not auto-owned"
    assert decision.allowed is False


def test_unknown_surface_defaults_denied(container):
    # surface_id omitted -> local-owner forced off (safe default).
    decision = guard_inbound(container, "stranger-123")
    assert decision is not None
    assert decision.allowed is False


@pytest.mark.parametrize("surface", ["cli", "repl", "local"])
def test_local_surface_operator_allowed(container, surface):
    # Trusted local surface + POLYROB_LOCAL -> owner, allowed (None == allow).
    assert guard_inbound(container, "operator", surface_id=surface) is None
