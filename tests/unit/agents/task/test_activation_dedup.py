"""Task 12 — activation dedup + hide gated skills from the model catalog.

Two independent, conservative wins:

1. ``get_catalog_skills``/``format_skill_catalog`` must EXCLUDE skills whose
   effective ``auto_activate`` is false (the model's auto-suggested menu never
   advertises a gated skill like ``polymarket-trading``/``hyperliquid-trading``),
   while such a skill remains reachable via seeded/persona force-include and
   ``load_skill`` once it IS present in a session's skill set.

2. ``build_load_skill_result(session_skills, skill_id, activated=None)`` dedupes
   repeated ``load_skill`` calls for the same id within a session: the first
   call returns the real body and records the id as activated; a subsequent
   call for the same id returns a short "already active" ack
   (``metadata.skill_already_active is True``) instead of re-emitting the body.

DECISION (documented, not a silent scope cut): a literal reading of the task
asked to also pre-mark seeded/persona force-included skills as "activated" at
session-construction time, before any load_skill call. Verified against the
real injection flow (agents/task/agent/core/construction.py +
skill_manager.format_skill_catalog): under the DEFAULT config
(SKILL_PROGRESSIVE_DISCLOSURE=on), a seeded skill's FULL BODY is never eagerly
injected anywhere -- only its one-line <skill-catalog> entry is -- so
pre-marking it "activated" before the model ever calls load_skill would make
its real instructions (e.g. the trading skills' >$500 confirmation-gate
procedure) permanently unreachable in that session. That is a functional/safety
regression, not a token-saving optimization, and directly conflicts with the
"must remain reachable... via seeded... force-include" money-critical guard.
So: seeded skills participate in the SAME dedup as any other skill (first
load_skill call delivers the body, the next one short-circuits) but are not
pre-activated with zero deliveries. See
test_seeded_skill_not_preactivated_but_dedupes_normally below.
"""
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.controller._helpers import build_load_skill_result
from agents.task.agent.skill_manager import SkillManager


# --- shared fixture builder (mirrors test_skill_rules_integrity.py's pattern) ---

def _make_manager(tmp_path: Path, rules: dict, bodies: dict) -> SkillManager:
    (tmp_path / "rules.json").write_text(json.dumps(rules))
    for sid, body in bodies.items():
        d = tmp_path / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body)
    return SkillManager(skills_dir=tmp_path)


# =====================================================================
# Item 2 — activation dedup (pure build_load_skill_result)
# =====================================================================

def test_load_skill_noops_if_already_injected():
    """Brief's own Step-1 sketch: a pre-populated `activated` set short-circuits
    immediately, even on what would otherwise be the "first" call."""
    session_skills = {"demo": "<body>"}
    activated = {"demo"}
    res = build_load_skill_result(session_skills, "demo", activated=activated)
    assert res.metadata.get("skill_already_active") is True


def test_build_load_skill_result_dedup_first_call_gives_body_second_short_circuits():
    session_skills = {"demo": SimpleNamespace(content="BODY TEXT")}
    activated = set()

    first = build_load_skill_result(session_skills, "demo", activated=activated)
    assert first.error is None
    assert "BODY TEXT" in first.extracted_content
    assert first.metadata == {"skill_loaded": "demo"}
    assert "demo" in activated, "a fresh load must record the id as activated"

    second = build_load_skill_result(session_skills, "demo", activated=activated)
    assert second.error is None
    assert "BODY TEXT" not in (second.extracted_content or ""), (
        "the body must not be re-emitted once the id is already active"
    )
    assert second.metadata.get("skill_already_active") is True


def test_build_load_skill_result_activated_none_disables_tracking_backward_compat():
    """Existing callers that don't pass `activated` (e.g. the pre-Task-12 call
    sites) keep the old always-return-the-body behavior, byte-identical."""
    session_skills = {"demo": SimpleNamespace(content="BODY TEXT")}
    first = build_load_skill_result(session_skills, "demo")
    second = build_load_skill_result(session_skills, "demo")
    assert "BODY TEXT" in first.extracted_content
    assert "BODY TEXT" in second.extracted_content
    assert second.metadata == {"skill_loaded": "demo"}


def test_build_load_skill_result_unknown_id_unaffected_by_activated():
    session_skills = {"demo": SimpleNamespace(content="BODY TEXT")}
    activated = set()
    res = build_load_skill_result(session_skills, "nope", activated=activated)
    assert res.error is not None
    assert "nope" not in activated


# =====================================================================
# Item 2 — activation dedup, wired through the REAL load_skill action
# =====================================================================

def _bare_controller():
    import agents.task.agent.service  # noqa: F401 -- avoid import cycle
    from tools.controller.registry.service import Registry
    from tools.controller.service import Controller

    c = object.__new__(Controller)
    c.logger = logging.getLogger("activation-dedup-test")
    c.registry = Registry()
    c.user_id = "tenant-A"
    c.session_id = "s1"
    c.output_model = None
    c._session_skills = {}
    c._activated_skills = set()
    return c


@pytest.mark.asyncio
async def test_load_skill_action_dedupes_across_repeated_calls(monkeypatch):
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "true")
    c = _bare_controller()
    c._session_skills = {"demo": SimpleNamespace(content="BODY TEXT")}
    c._register_default_actions()

    assert "load_skill" in c.registry.registry.actions
    action = c.registry.registry.actions["load_skill"]
    params = action.param_model(skill_id="demo")

    r1 = await action.function(params, execution_context=None)
    assert "BODY TEXT" in r1.extracted_content
    r2 = await action.function(params, execution_context=None)
    assert "BODY TEXT" not in (r2.extracted_content or "")
    assert r2.metadata.get("skill_already_active") is True


@pytest.mark.asyncio
async def test_load_skill_action_dedup_is_per_controller_instance(monkeypatch):
    """Session-scoped: a second, independent Controller (i.e. a different session)
    must NOT inherit the first session's activation state."""
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "true")

    c1 = _bare_controller()
    c1._session_skills = {"demo": SimpleNamespace(content="BODY TEXT")}
    c1._register_default_actions()
    action1 = c1.registry.registry.actions["load_skill"]
    await action1.function(action1.param_model(skill_id="demo"), execution_context=None)

    c2 = _bare_controller()
    c2._session_skills = {"demo": SimpleNamespace(content="BODY TEXT")}
    c2._register_default_actions()
    action2 = c2.registry.registry.actions["load_skill"]
    r = await action2.function(action2.param_model(skill_id="demo"), execution_context=None)
    assert "BODY TEXT" in r.extracted_content, (
        "a fresh session/Controller must not see another session's activated set"
    )


# =====================================================================
# Item 1 — hide gated skills from the model catalog
# =====================================================================

def test_gated_skill_absent_from_get_catalog_skills(tmp_path):
    sm = _make_manager(
        tmp_path,
        rules={
            "gated-skill": {
                "auto_activate": False,
                "triggers": {"keywords": ["trade"]},
                "priority": 5,
                "description": "GATED: dangerous trading skill",
            },
            "normal-skill": {
                "auto_activate": True,
                "triggers": {"keywords": ["normal"]},
                "priority": 5,
                "description": "an ordinary skill",
            },
        },
        bodies={
            "gated-skill": "# Gated Skill\n\nDangerous trading instructions.",
            "normal-skill": "# Normal Skill\n\nSafe instructions.",
        },
    )
    catalog = sm.get_catalog_skills()
    ids = {s.skill_id for s in catalog}
    assert "gated-skill" not in ids
    assert "normal-skill" in ids


def test_gated_skill_absent_from_catalog_text(tmp_path):
    sm = _make_manager(
        tmp_path,
        rules={
            "gated-skill": {
                "auto_activate": False,
                "triggers": {"keywords": ["trade"]},
                "priority": 5,
                "description": "GATED: dangerous trading skill",
            },
        },
        bodies={"gated-skill": "# Gated Skill\n\nDangerous trading instructions."},
    )
    catalog = sm.get_catalog_skills()
    text = sm.format_skill_catalog(catalog)
    assert "gated-skill" not in text
    assert "Dangerous trading instructions" not in text


def test_gated_skill_not_trigger_matched_even_on_keyword_hit(tmp_path):
    """A gated skill must not auto-activate even when its own keyword is present
    in the task text -- the ONLY path in is explicit seeding."""
    sm = _make_manager(
        tmp_path,
        rules={
            "gated-skill": {
                "auto_activate": False,
                "triggers": {"keywords": ["trade now"]},
                "priority": 5,
                "description": "GATED",
            },
        },
        bodies={"gated-skill": "# Gated Skill\n\nBody."},
    )
    matched = sm.get_skills_for_session(task="please trade now immediately")
    assert all(m.skill_id != "gated-skill" for m in matched)


def test_gated_skill_still_reachable_via_seeded_force_include_and_load_skill(tmp_path):
    """Money-critical guard: hiding a gated skill from the catalog must NOT make
    it unreachable via the seeded/persona force-include path, nor via load_skill
    once it is part of the session's skill set."""
    sm = _make_manager(
        tmp_path,
        rules={
            "gated-skill": {
                "auto_activate": False,
                "triggers": {"keywords": ["trade"]},
                "priority": 5,
                "description": "GATED: dangerous trading skill",
            },
        },
        bodies={"gated-skill": "# Gated Skill\n\nDangerous trading instructions."},
    )
    # Seeded (persona force-include) bypasses the auto_activate gate by design.
    seeded = sm.get_skills_for_session(task="", seeded_skill_ids=["gated-skill"])
    seeded_ids = {m.skill_id for m in seeded}
    assert "gated-skill" in seeded_ids

    # Still confirm it stayed OUT of the generic catalog (seeding is per-session,
    # not a change to the shared catalog).
    catalog_ids = {s.skill_id for s in sm.get_catalog_skills()}
    assert "gated-skill" not in catalog_ids

    # And still loadable by id via load_skill once present in session_skills.
    session_skills = {m.skill_id: m for m in seeded}
    result = build_load_skill_result(session_skills, "gated-skill")
    assert result.error is None
    assert "Dangerous trading instructions" in result.extracted_content


def test_seeded_skill_not_preactivated_but_dedupes_normally(tmp_path):
    """Documents the DECISION at the top of this file: seeded skills are not
    pre-marked activated at construction time (their body has never actually
    been shown yet under progressive disclosure), but DO dedupe like any other
    skill once load_skill is actually called."""
    sm = _make_manager(
        tmp_path,
        rules={
            "seeded-skill": {
                "auto_activate": False,
                "triggers": {},
                "priority": 5,
                "description": "seeded only",
            },
        },
        bodies={"seeded-skill": "# Seeded Skill\n\nReal instructions."},
    )
    matched = sm.get_skills_for_session(task="", seeded_skill_ids=["seeded-skill"])
    session_skills = {m.skill_id: m for m in matched}
    activated: set = set()  # fresh -- construction.py does not pre-seed this

    first = build_load_skill_result(session_skills, "seeded-skill", activated=activated)
    assert "Real instructions" in first.extracted_content, (
        "a seeded skill's FIRST load_skill call must deliver the real body -- "
        "it has never actually been shown to the model under progressive disclosure"
    )
    second = build_load_skill_result(session_skills, "seeded-skill", activated=activated)
    assert second.metadata.get("skill_already_active") is True
