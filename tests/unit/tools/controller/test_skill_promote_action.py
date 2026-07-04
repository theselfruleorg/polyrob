"""P3-3 — owner-gated `.pending → active` promotion via skill_manage action='promote'.

The promotion primitive (SkillManager.promote_pending_skill) existed but had no caller.
This wires it as an owner-only action: a forged/leaf turn can never self-promote.
"""
import logging
import types

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from agents.task.agent.skill_manager import SkillManager
from agents.task.agent.skill_writer import PROVENANCE_BACKGROUND
from tools.controller.registry.service import Registry
from tools.controller.service import Controller

_BODY = "# Pending Skill\n\nA reusable procedure awaiting review, long enough to pass.\n"


def _controller(sm, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)
    c = object.__new__(Controller)
    c.logger = logging.getLogger("skill-promote-test")
    c.registry = Registry()
    c.user_id = "u1"
    c.session_id = "s1"
    c._register_skill_manage_action()
    return c


def _seed_pending(sm):
    res = sm.create_skill("draft-skill", _BODY, user_id="u1",
                          created_by=PROVENANCE_BACKGROUND)  # forced .pending
    assert res.pending


@pytest.mark.asyncio
async def test_owner_can_promote_pending(monkeypatch, tmp_path):
    sm = SkillManager(skills_dir=tmp_path)
    _seed_pending(sm)
    c = _controller(sm, monkeypatch)
    action = c.registry.registry.actions["skill_manage"]
    ctx = types.SimpleNamespace(user_id="u1", is_sub_agent=False, role="orchestrator")
    params = action.param_model(action="promote", skill_id="draft-skill")
    res = await action.function(params, execution_context=ctx)
    assert res.error is None, res.error
    assert (tmp_path / "user_u1" / "draft-skill" / "SKILL.md").exists()
    assert not (tmp_path / "user_u1" / ".pending" / "draft-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_forged_turn_cannot_promote(monkeypatch, tmp_path):
    sm = SkillManager(skills_dir=tmp_path)
    _seed_pending(sm)
    c = _controller(sm, monkeypatch)
    action = c.registry.registry.actions["skill_manage"]
    ctx = types.SimpleNamespace(user_id="u1", is_sub_agent=True, role="leaf")
    params = action.param_model(action="promote", skill_id="draft-skill")
    res = await action.function(params, execution_context=ctx)
    assert res.error and "owner" in res.error.lower()
    # still pending, never activated
    assert (tmp_path / "user_u1" / ".pending" / "draft-skill" / "SKILL.md").exists()
    assert not (tmp_path / "user_u1" / "draft-skill" / "SKILL.md").exists()
