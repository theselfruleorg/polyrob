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


@pytest.mark.asyncio
async def test_sub_agent_author_is_quarantined_even_review_off(monkeypatch, tmp_path):
    """CRITICAL regression: a sub-agent (background-review) author must be detected via
    execution_context.is_sub_agent/role and ALWAYS quarantined — even with
    SKILLS_WRITABLE_REQUIRE_REVIEW=false. (The old message_kind/_turn_kind signal was
    dead code → forged authors could auto-activate.)"""
    import types
    from agents.task.agent.skill_manager import SkillManager
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "false")

    sm = SkillManager(skills_dir=tmp_path)
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)

    c = _bare_controller()
    c._register_skill_manage_action()
    action = c.registry.registry.actions["skill_manage"]
    ctx = types.SimpleNamespace(user_id="u1", is_sub_agent=True, role="leaf")
    params = action.param_model(
        action="create", skill_id="auto-skill",
        content="# Auto Skill\n\nA reusable procedure distilled from the run, long enough.\n",
    )
    res = await action.function(params, execution_context=ctx)
    assert res.extracted_content and "pending review" in res.extracted_content
    # proof on disk: pending, NOT active
    assert (tmp_path / "user_u1" / ".pending" / "auto-skill" / "SKILL.md").exists()
    assert not (tmp_path / "user_u1" / "auto-skill" / "SKILL.md").exists()
