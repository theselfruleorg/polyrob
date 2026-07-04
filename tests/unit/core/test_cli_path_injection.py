"""CLI container injects a project-scoped PathManager; workspace == cwd (R1)."""
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_cli_container_workspace_is_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # lazy validation, no call
    # Prevent real LLM API calls during container boot.
    with patch("modules.llm.llm_manager.LLMManager._initialize", new=AsyncMock()):
        from core.bootstrap import build_cli_container
        container = await build_cli_container()
    pm_service = container.get_service("path_manager")
    assert pm_service is not None
    assert pm_service.get_workspace_dir("s1", "local") == tmp_path.resolve()


@pytest.mark.asyncio
async def test_cli_container_shares_global_pm_singleton_no_env_shim(tmp_path, monkeypatch):
    """Fix 3: the injected path_manager IS the global pm() singleton, and no
    DATA_ROOT env shim is used."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("DATA_ROOT", raising=False)
    with patch("modules.llm.llm_manager.LLMManager._initialize", new=AsyncMock()):
        from core.bootstrap import build_cli_container
        container = await build_cli_container()
    from agents.task.path import pm
    injected = container.get_service("path_manager")
    # Single source of truth: pm() returns the very same instance.
    assert pm() is injected
    # No env-var shim: build_cli_container must not set DATA_ROOT.
    import os
    assert os.environ.get("DATA_ROOT") is None
    # Unported pm() utility call sites now resolve under .polyrob, not ./data/task.
    feed = pm().get_subdir("sess1", "feed", "local")
    assert str(feed).startswith(str((tmp_path / ".polyrob" / "sessions").resolve()))
    assert "data/task" not in str(feed)
