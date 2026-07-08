"""SK-F10: a self-wake / async-delegation-result re-entry into the MAIN agent
resolves to execution_context.is_sub_agent=False, role="orchestrator" — the
SAME shape as a genuine owner turn. Without a turn-kind signal, the forged
turn could auto-activate a skill (review off) and self-promote its own
pending draft, and self_context_manage promote had the identical hole.

Fix: the run loop stamps `execution_context.metadata["turn_kind"]` from the
orchestrator's `_forged_turn_kind` marker (set when a drained HITL message
is kind="self_wake"/"delegation_result", cleared on a genuine turn).
`_is_forged_or_autonomous_turn` treats that turn_kind as forged, closing the
gap without touching the is_sub_agent/role/autonomous checks already fixed
under C7.
"""
import logging
import types

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from tools.controller.action_registration import _is_forged_or_autonomous_turn
from agents.task.agent.skill_manager import SkillManager
from agents.task.agent.skill_writer import PROVENANCE_BACKGROUND
from tools.controller.registry.service import Registry
from tools.controller.service import Controller

_BODY = "# Pending Skill\n\nA reusable procedure awaiting review, long enough to pass.\n"


def _ctx(turn_kind=None, role="orchestrator", is_sub=False, session_id="s1", user_id="u1"):
    return types.SimpleNamespace(
        role=role,
        is_sub_agent=is_sub,
        session_id=session_id,
        user_id=user_id,
        metadata={"turn_kind": turn_kind} if turn_kind is not None else {},
    )


def _controller(sm, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)
    c = object.__new__(Controller)
    c.logger = logging.getLogger("forged-turn-gate-test")
    c.registry = Registry()
    c.user_id = "u1"
    c.session_id = "s1"
    c._register_skill_manage_action()
    return c


def _seed_pending(sm):
    res = sm.create_skill("draft-skill", _BODY, user_id="u1", created_by=PROVENANCE_BACKGROUND)
    assert res.pending


# ---------------------------------------------------------------------------
# Pure gate function
# ---------------------------------------------------------------------------

def test_self_wake_turn_kind_is_forged():
    assert _is_forged_or_autonomous_turn(_ctx(turn_kind="self_wake"), None) is True


def test_delegation_result_turn_kind_is_forged():
    assert _is_forged_or_autonomous_turn(_ctx(turn_kind="delegation_result"), None) is True


def test_producer_kind_is_member_of_forged_turn_kinds():
    """A3: the async-delegation producer kind must be a member of FORGED_TURN_KINDS
    so producer, marker-recompute, and gate stay single-sourced."""
    from agents.task.agent.core.self_wake import (
        DELEGATION_RESULT_KIND,
        SELF_WAKE_KIND,
        FORGED_TURN_KINDS,
    )

    assert DELEGATION_RESULT_KIND in FORGED_TURN_KINDS
    assert SELF_WAKE_KIND in FORGED_TURN_KINDS


def test_continuation_turn_kind_is_not_forged():
    assert _is_forged_or_autonomous_turn(
        _ctx(turn_kind="continuation", session_id="plain-not-forged"), None
    ) is False


def test_none_turn_kind_orchestrator_is_not_forged():
    assert _is_forged_or_autonomous_turn(
        _ctx(turn_kind=None, session_id="plain-not-forged-2"), None
    ) is False


# ---------------------------------------------------------------------------
# skill_manage: promote denied + create quarantined even with review off
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forged_self_wake_cannot_promote(monkeypatch, tmp_path):
    sm = SkillManager(skills_dir=tmp_path)
    _seed_pending(sm)
    c = _controller(sm, monkeypatch)
    action = c.registry.registry.actions["skill_manage"]
    ctx = _ctx(turn_kind="self_wake", role="orchestrator", is_sub=False)
    params = action.param_model(action="promote", skill_id="draft-skill")
    res = await action.function(params, execution_context=ctx)
    assert res.error and "owner" in res.error.lower()
    assert (tmp_path / "user_u1" / ".pending" / "draft-skill" / "SKILL.md").exists()
    assert not (tmp_path / "user_u1" / "draft-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_forged_self_wake_create_quarantined_even_review_off(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "false")
    sm = SkillManager(skills_dir=tmp_path)
    c = _controller(sm, monkeypatch)
    action = c.registry.registry.actions["skill_manage"]
    ctx = _ctx(turn_kind="self_wake", role="orchestrator", is_sub=False)
    params = action.param_model(
        action="create", skill_id="forged-skill",
        content="# Forged Skill\n\nA reusable procedure distilled from a forged turn.\n",
    )
    res = await action.function(params, execution_context=ctx)
    assert res.extracted_content and "pending review" in res.extracted_content
    assert (tmp_path / "user_u1" / ".pending" / "forged-skill" / "SKILL.md").exists()
    assert not (tmp_path / "user_u1" / "forged-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_genuine_turn_create_auto_activates_when_review_off(monkeypatch, tmp_path):
    """Unchanged-behavior guard: a genuine user turn (no turn_kind) with review off
    still auto-activates — the forged gate must not regress the happy path."""
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "false")
    sm = SkillManager(skills_dir=tmp_path)
    c = _controller(sm, monkeypatch)
    action = c.registry.registry.actions["skill_manage"]
    ctx = _ctx(turn_kind=None, role="orchestrator", is_sub=False)
    params = action.param_model(
        action="create", skill_id="genuine-skill",
        content="# Genuine Skill\n\nA reusable procedure from a real user turn.\n",
    )
    res = await action.function(params, execution_context=ctx)
    assert res.error is None, res.error
    assert (tmp_path / "user_u1" / "genuine-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_genuine_owner_turn_can_promote(monkeypatch, tmp_path):
    # T3-02: promote is owner-only. A genuine turn whose uid is the BOUND OWNER
    # principal can promote.
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    sm = SkillManager(skills_dir=tmp_path)
    _seed_pending(sm)
    c = _controller(sm, monkeypatch)
    action = c.registry.registry.actions["skill_manage"]
    ctx = _ctx(turn_kind=None, role="orchestrator", is_sub=False)  # user_id="u1"
    params = action.param_model(action="promote", skill_id="draft-skill")
    res = await action.function(params, execution_context=ctx)
    assert res.error is None, res.error
    assert (tmp_path / "user_u1" / "draft-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_non_owner_genuine_turn_cannot_promote(monkeypatch, tmp_path):
    # T3-02 (the closed hole): a genuine (non-forged) turn that is NOT the owner —
    # the skill promote branch previously gated on is_forged ONLY, so the agent could
    # create (-> .pending under REQUIRE_REVIEW) then promote in the SAME turn, activating
    # an unreviewed body (e.g. an injected "author skill X and promote it"). Mirror the
    # self_context_manage owner gate: no owner principal + not local => refused.
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("BOT_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("SURFACE_SUPER_ADMIN_USER_IDS", raising=False)
    sm = SkillManager(skills_dir=tmp_path)
    _seed_pending(sm)
    c = _controller(sm, monkeypatch)
    action = c.registry.registry.actions["skill_manage"]
    ctx = _ctx(turn_kind=None, role="orchestrator", is_sub=False)  # user_id="u1", not owner
    params = action.param_model(action="promote", skill_id="draft-skill")
    res = await action.function(params, execution_context=ctx)
    assert res.error and "owner" in res.error.lower()
    # draft stays quarantined; not activated
    assert (tmp_path / "user_u1" / ".pending" / "draft-skill" / "SKILL.md").exists()
    assert not (tmp_path / "user_u1" / "draft-skill" / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# Marker lifecycle: orchestrator._forged_turn_kind set on forged drain,
# cleared on a genuine-kind drain, and stamped into execution_context.
# ---------------------------------------------------------------------------

def test_build_execution_context_stamps_turn_kind_from_orchestrator_marker():
    """Agent._build_execution_context (step_execution.py) must read the
    orchestrator's `_forged_turn_kind` marker into metadata['turn_kind']."""
    from agents.task.agent.core.step_execution import StepExecutionMixin

    class _FakeOrchestrator:
        workspace_dir = "/tmp/ws"

    class _FakeAgent(StepExecutionMixin):
        def __init__(self, forged_kind):
            self.orchestrator = _FakeOrchestrator()
            self.orchestrator._forged_turn_kind = forged_kind
            self.agent_id = "a1"
            self._is_sub_agent = False
            self._role = "orchestrator"
            self._parent_session_id = None
            self.effective_session_id = "s1"
            self.user_id = "u1"
            self.available_file_paths = []
            self.sensitive_data = {}

    forged = _FakeAgent("self_wake")
    ctx = forged._build_execution_context(browser_context=None)
    assert ctx.metadata.get("turn_kind") == "self_wake"

    genuine = _FakeAgent(None)
    ctx2 = genuine._build_execution_context(browser_context=None)
    assert ctx2.metadata.get("turn_kind") is None


@pytest.mark.asyncio
async def test_drain_sets_forged_marker_and_genuine_clears_it():
    """_drain_user_messages (user_ingress.py) must set orchestrator._forged_turn_kind
    when the drained batch is a forged kind, and clear it (set to None) when the
    drained batch is a genuine kind (e.g. "continuation") — so a self-wake turn
    can't leave the marker stuck for a later real turn."""
    from agents.task.agent.core.user_ingress import UserIngressMixin

    class _FakeHITL:
        def __init__(self, batches):
            self._batches = list(batches)

        async def drain_user_messages(self):
            return self._batches.pop(0) if self._batches else []

    class _FakeOrchestrator:
        pass

    class _FakeWorkspaceContext:
        def get_workspace_changes(self, **kwargs):
            class _WC:
                def has_changes(self_inner):
                    return False
            return _WC()

    class _FakeAgent(UserIngressMixin):
        def __init__(self, batches):
            self.hitl_manager = _FakeHITL(batches)
            self.orchestrator = _FakeOrchestrator()
            self.session_id = "s1"
            self.user_id = "u1"
            self.session_manager = None
            self.workspace_context = _FakeWorkspaceContext()
            self.logger = logging.getLogger("drain-test")

    agent = _FakeAgent([
        [{"text": "wake up", "kind": "self_wake", "metadata": {}}],
        [{"text": "hi", "kind": "continuation", "metadata": {}}],
    ])

    await agent._drain_user_messages()
    assert agent.orchestrator._forged_turn_kind == "self_wake"

    await agent._drain_user_messages()
    assert agent.orchestrator._forged_turn_kind is None
