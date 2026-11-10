"""W2-B — skill_manage action gating + tenant guard."""
import logging

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from tools.controller.registry.service import Registry
from tools.controller.service import Controller


def _bare_controller():
    c = object.__new__(Controller)
    c.logger = logging.getLogger("skill-manage-test")
    c.registry = Registry()
    c.user_id = "tenant-A"
    c.session_id = "s1"
    return c


def test_suppressed_when_flag_off(monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE", "false")
    c = _bare_controller()
    c._register_skill_manage_action()
    assert "skill_manage" not in c.registry.registry.actions


def test_registered_when_flag_on(monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    c = _bare_controller()
    c._register_skill_manage_action()
    assert "skill_manage" in c.registry.registry.actions


@pytest.mark.asyncio
async def test_requires_user(monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    c = _bare_controller()
    c.user_id = None
    c._register_skill_manage_action()
    action = c.registry.registry.actions["skill_manage"]
    params = action.param_model(action="create", skill_id="x", content="# T\n\nbody")
    res = await action.function(params, execution_context=None)
    assert res.error and "tenant" in res.error.lower()


def _sub_agent_call(monkeypatch, tmp_path, ctx):
    from agents.task.agent.skill_manager import SkillManager
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    sm = SkillManager(skills_dir=tmp_path)
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)
    c = _bare_controller()
    c._register_skill_manage_action()
    action = c.registry.registry.actions["skill_manage"]
    params = action.param_model(
        action="create", skill_id="auto-skill", when="When a run repeats this",
        reason="test reason",
        content="# Auto Skill\n\nA reusable procedure distilled from the run, long enough.\n",
    )
    return action.function(params, execution_context=ctx)


@pytest.mark.asyncio
async def test_sub_agent_author_on_a_non_owner_tenant_stays_a_draft(monkeypatch, tmp_path):
    """A sub-agent (background-review) author on a tenant that is not the owner is
    detected via execution_context and lands a draft."""
    import types
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "someone-else")
    ctx = types.SimpleNamespace(user_id="u1", is_sub_agent=True, role="leaf")
    res = await _sub_agent_call(monkeypatch, tmp_path, ctx)
    assert res.extracted_content and "pending review" in res.extracted_content
    # 2026-10-14: "saved (pending review)" read as done — the agent told the owner a
    # promo-bounty rule was "Fixed" while it sat in .pending. Say it is inert.
    assert "NOT YET IN EFFECT" in res.extracted_content
    assert "saved (pending review)" not in res.extracted_content
    # proof on disk: pending, NOT active
    assert (tmp_path / "user_u1" / ".pending" / "auto-skill" / "SKILL.md").exists()
    assert not (tmp_path / "user_u1" / "auto-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_sub_agent_author_on_the_owner_tenant_is_active(monkeypatch, tmp_path):
    import types
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    ctx = types.SimpleNamespace(user_id="u1", is_sub_agent=True, role="leaf")
    res = await _sub_agent_call(monkeypatch, tmp_path, ctx)
    assert res.error is None, res.error
    assert (tmp_path / "user_u1" / "auto-skill" / "SKILL.md").exists()
    assert not (tmp_path / "user_u1" / ".pending" / "auto-skill").exists()


@pytest.mark.asyncio
async def test_room_turn_on_the_owner_tenant_stays_a_draft(monkeypatch, tmp_path):
    import types
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    monkeypatch.setattr("core.security.owner_turn.is_room_turn", lambda ctx: True)
    ctx = types.SimpleNamespace(user_id="u1", is_sub_agent=False, role="orchestrator", metadata={})
    res = await _sub_agent_call(monkeypatch, tmp_path, ctx)
    assert res.extracted_content and "pending review" in res.extracted_content
    assert (tmp_path / "user_u1" / ".pending" / "auto-skill" / "SKILL.md").exists()
    assert not (tmp_path / "user_u1" / "auto-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_correspondent_tainted_session_stays_a_draft(monkeypatch, tmp_path):
    import types
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    monkeypatch.setattr("core.security.taints.correspondent_tainted", lambda orch: True)
    ctx = types.SimpleNamespace(user_id="u1", is_sub_agent=False, role="orchestrator", metadata={})
    res = await _sub_agent_call(monkeypatch, tmp_path, ctx)
    assert res.extracted_content and "pending review" in res.extracted_content
    assert (tmp_path / "user_u1" / ".pending" / "auto-skill" / "SKILL.md").exists()
    assert not (tmp_path / "user_u1" / "auto-skill" / "SKILL.md").exists()
