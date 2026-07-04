"""IdentityProvider seam: local mode is a single fixed identity (R2)."""
import pytest
from unittest.mock import AsyncMock, patch
from core.identity import IdentityProvider, LocalIdentity, ConstantIdentity


def test_local_identity_resolves_local():
    idp = LocalIdentity()
    assert isinstance(idp, IdentityProvider)
    assert idp.resolve() == "local"


def test_constant_identity():
    assert ConstantIdentity("alice").resolve() == "alice"


@pytest.mark.asyncio
async def test_cli_container_registers_local_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with patch("modules.llm.llm_manager.LLMManager._initialize", AsyncMock()):
        from core.bootstrap import build_cli_container
        c = await build_cli_container()
    assert c.get_service("identity").resolve() == "local"
