"""Task 12: one identity oracle on the CLI + no anon fallback in goal/cron tools.

ME-D3: build_cli_container's registered "identity" service must resolve to the SAME
tenant as core.identity.resolve_identity() (owner principal if bound, else "local"),
so REPL chat sessions and `polyrob goals`/objectives share one tenant key.

ME-D4: GoalTool._user / CronJobTool._user must never fall back to the anonymous
sentinel "_anonymous_" when execution_context.user_id is falsy.
"""
import pytest
from unittest.mock import AsyncMock, patch

from core.identity import resolve_identity, ANON_USER_ID


class _CtxNoUser:
    user_id = None


@pytest.mark.asyncio
async def test_cli_container_identity_matches_resolve_identity_with_owner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    with patch("modules.llm.llm_manager.LLMManager._initialize", AsyncMock()):
        from core.bootstrap import build_cli_container
        container = await build_cli_container()
    assert resolve_identity() == "rob"
    assert container.get_service("identity").resolve() == "rob"


@pytest.mark.asyncio
async def test_cli_container_identity_falls_back_to_local_without_owner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("BOT_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("SURFACE_SUPER_ADMIN_USER_IDS", raising=False)
    with patch("modules.llm.llm_manager.LLMManager._initialize", AsyncMock()):
        from core.bootstrap import build_cli_container
        container = await build_cli_container()
    assert resolve_identity() == "local"
    assert container.get_service("identity").resolve() == "local"


def test_goal_tool_user_never_anonymous(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)
    from tools.goal_tools import GoalTool
    result = GoalTool._user(_CtxNoUser())
    assert result != ANON_USER_ID
    assert result == resolve_identity()


def test_goal_tool_user_prefers_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    from tools.goal_tools import GoalTool
    assert GoalTool._user(_CtxNoUser()) == "rob"


def test_goal_tool_user_passthrough_when_set():
    from tools.goal_tools import GoalTool
    class Ctx:
        user_id = "alice"
    assert GoalTool._user(Ctx()) == "alice"


def test_cronjob_tool_user_never_anonymous(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)
    from tools.cronjob_tools import CronJobTool
    result = CronJobTool._user(_CtxNoUser())
    assert result != ANON_USER_ID
    assert result == resolve_identity()


def test_cronjob_tool_user_prefers_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    from tools.cronjob_tools import CronJobTool
    assert CronJobTool._user(_CtxNoUser()) == "rob"
