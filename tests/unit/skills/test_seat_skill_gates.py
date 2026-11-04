"""Review 2026-11-03 (skills system): the seat gate, owner-only skills, money
prerequisites and the builtin body cache.

* H1/M5 — a room or a correspondent runs under the OWNER's user_id, so the tenant
  check alone let it list and load the owner's authored skills (programme terms)
  and every ecosystem skill (``~/.claude/skills``). A third-party seat gets the
  shipped library only.
* M5(c) — ``load_skill``'s fallback served an ecosystem skill the index scan removed.
* M6 — owner-only is the frontmatter key ``polyrob-owner-only`` (one source).
* M2 — the persona seeds pass the same seat gate.
* M3 — a pinned money playbook brings its safety prerequisites.
* M7 — a builtin body changed on disk is not served stale.
* MED-1/LOW-2..4 (re-validation) — the seat is asked per call; pins and
  prerequisites on a third-party seat pass the full seat gate.
"""
import logging
import types
from pathlib import Path

import pytest

from agents.task.agent.skill_manager import SkillManager
from core.skills import scopes
from core.skills.frontmatter import parse_frontmatter
from modules.skills.writer import PROVENANCE_USER
from tests.support.skill_library import write_library

ROOT = Path(__file__).resolve().parents[3]

AUTHORED = ("---\nname: my-programme\ndescription: The owner's programme terms.\n"
            "metadata:\n  polyrob-when: 'Running the owner programme'\n---\n"
            "# My programme\n\nPay 5 units per accepted entry. " + "terms " * 10)
ECOSYSTEM = ("---\nname: eco-skill\ndescription: An ecosystem skill.\n---\n"
             "# Eco\n\nAn operator's own skill. " + "body " * 10)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    eco = tmp_path / "home" / ".claude" / "skills"
    (eco / "eco-skill").mkdir(parents=True)
    (eco / "eco-skill" / "SKILL.md").write_text(ECOSYSTEM, encoding="utf-8")
    monkeypatch.setattr(scopes, "user_external_roots", lambda: [eco])
    monkeypatch.setattr(scopes, "skills_data_home", lambda: tmp_path / "data")
    return tmp_path


def _manager_with_authored():
    sm = SkillManager()
    res = sm.create_skill("my-programme", AUTHORED, user_id="u1",
                          created_by=PROVENANCE_USER, pending=False)
    assert res.ok, res.errors
    return sm


def _ids(sm, **kw):
    return {m.skill_id for m in sm.get_catalog_skills(user_id="u1", max_skills=None, **kw)}


def test_owner_seat_lists_and_loads_its_own_and_ecosystem_skills(homes):
    sm = _manager_with_authored()
    ids = _ids(sm, tool_ids=[])
    assert {"my-programme", "eco-skill"} <= ids
    assert sm.may_load_skill("my-programme", tool_ids=[], user_id="u1")
    assert sm.may_load_skill("eco-skill", tool_ids=[], user_id="u1")


def test_third_party_seat_never_lists_or_loads_owner_or_ecosystem_skills(homes):
    sm = _manager_with_authored()
    ids = _ids(sm, tool_ids=[], third_party=True)
    assert "my-programme" not in ids and "eco-skill" not in ids
    assert "web-research" in ids or "document-writing" in ids  # the library stays
    assert not sm.may_load_skill("my-programme", tool_ids=[], user_id="u1", third_party=True)
    assert not sm.may_load_skill("eco-skill", tool_ids=[], user_id="u1", third_party=True)
    assert sm.may_load_skill("document-writing", tool_ids=[], user_id="u1", third_party=True)


def test_load_fallback_refuses_an_ecosystem_skill_the_scan_removed(homes, monkeypatch):
    monkeypatch.setattr("modules.memory.task.threat_scan.is_suspicious",
                        lambda text: "operator's own" in text)
    monkeypatch.setattr("core.security.threat_report.report_threat", lambda *a, **k: None)
    sm = SkillManager()
    assert "eco-skill" not in _ids(sm, tool_ids=[])
    assert not sm.may_load_skill("eco-skill", tool_ids=[], user_id="u1")


# ---- M6: owner-only is a frontmatter key -------------------------------------

OWNER_ONLY = {"skill-authoring", "self-deploy", "incident-response", "pay-for-work",
              "get-paid-for-work", "polyrob-user-guide", "skill-security-review",
              "harness-manual", "person-analyzer"}
SHIPPED = sorted([*ROOT.glob("data/prompts/skills/*/SKILL.md"),
                  *ROOT.glob("packs/*/polyrob_*/skills/*/SKILL.md")])


def test_owner_only_comes_from_the_frontmatter():
    from core.skills.catalog import is_owner_only, rule_from_frontmatter
    marked = {md.parent.name for md in SHIPPED
              if is_owner_only(rule_from_frontmatter(
                  parse_frontmatter(md.read_text(encoding="utf-8"))[0]))}
    assert OWNER_ONLY <= marked, OWNER_ONLY - marked
    import core.skills.catalog as catalog
    assert not hasattr(catalog, "OWNER_ONLY_SKILLS"), "one source: the frontmatter"


def test_owner_only_skills_leave_a_third_party_index():
    sm = SkillManager()
    owner = {m.skill_id for m in sm.get_catalog_skills(tool_ids=["goal", "cronjob"],
                                                       max_skills=None)}
    room = {m.skill_id for m in sm.get_catalog_skills(tool_ids=["goal", "cronjob"],
                                                      max_skills=None, third_party=True)}
    assert {"skill-authoring", "polyrob-user-guide", "harness-manual"} <= owner
    assert not room & OWNER_ONLY
    assert not sm.may_load_skill("polyrob-user-guide", tool_ids=[], third_party=True)


# ---- M2: persona seeds pass the seat gate -------------------------------------

def test_persona_seeds_pass_the_seat_gate():
    from agents.task.agent.core.seat_skills import seat_persona_skills
    agent = types.SimpleNamespace(logger=logging.getLogger("t"))
    sm = SkillManager()
    seeds = ["coding-workflow", "skill-authoring"]
    assert seat_persona_skills(agent, sm, seeds, tool_ids=["coding"], user_id=None,
                               third_party=False, absent=()) == seeds
    assert seat_persona_skills(agent, sm, seeds, tool_ids=["coding"], user_id=None,
                               third_party=True, absent=()) == ["coding-workflow"]


def _stub_agent(*, public=False, tainted=False):
    orch = types.SimpleNamespace(user_id=None, session_id="room-session",
                                 _public_session=public, _correspondent_session=tainted)
    controller = types.SimpleNamespace(
        orchestrator=orch, list_tools=lambda: ["coding"], absent_tool_ids=lambda ids: set(),
        _activated_skills=set())
    return types.SimpleNamespace(orchestrator=orch, controller=controller,
                                 logger=logging.getLogger("t"), _profile_system_message=None,
                                 session_config=None)


def test_construction_filters_persona_seeds_on_a_room(monkeypatch):
    """Behavioural: _init_skills on a room seat never pins an owner-only persona seed."""
    from agents.task.agent.core.construction import AgentConstructionMixin
    monkeypatch.setattr("agents.task.templates.seeded_skills_for",
                        lambda persona: ["coding-workflow", "skill-authoring"])
    seen = {}
    real = SkillManager.get_pinned_skills

    def spy(self, seeded_skill_ids=None, **kw):
        seen["seeds"] = list(seeded_skill_ids or [])
        return real(self, seeded_skill_ids=seeded_skill_ids, **kw)
    monkeypatch.setattr(SkillManager, "get_pinned_skills", spy)
    agent = _stub_agent(public=True)
    AgentConstructionMixin._init_skills(agent, types.SimpleNamespace())
    assert "skill-authoring" not in seen["seeds"]
    assert "coding-workflow" in seen["seeds"]
    assert agent.controller._third_party_seat is True


def test_construction_sets_the_seat_flag_before_a_later_raise(monkeypatch):
    """MED-1(b): an exception after the seat is known still leaves the flag set."""
    from agents.task.agent.core.construction import AgentConstructionMixin

    def boom(*a, **k):
        raise RuntimeError("pin failure")
    monkeypatch.setattr(SkillManager, "get_pinned_skills", boom)
    agent = _stub_agent(public=True)
    AgentConstructionMixin._init_skills(agent, types.SimpleNamespace())
    assert agent.controller._third_party_seat is True


# ---- MED-1: the seat is asked per call, not snapshotted --------------------------

def test_seat_is_third_party_follows_live_taint():
    from agents.task.session_class import seat_is_third_party
    orch = types.SimpleNamespace(_public_session=False)
    controller = types.SimpleNamespace(orchestrator=orch, _third_party_seat=False)
    assert seat_is_third_party(controller) is False
    orch._correspondent_tainted = True  # hitl_ingress _set_correspondent_taint
    assert seat_is_third_party(controller) is True


def test_seat_is_third_party_snapshot_room_and_missing_flag():
    from agents.task.session_class import seat_is_third_party
    owner = types.SimpleNamespace(_public_session=False)
    assert seat_is_third_party(types.SimpleNamespace(orchestrator=owner)) is False
    assert seat_is_third_party(types.SimpleNamespace(
        orchestrator=owner, _third_party_seat=True)) is True
    room = types.SimpleNamespace(_public_session=True)
    assert seat_is_third_party(types.SimpleNamespace(orchestrator=room)) is True


def test_append_prerequisites_asks_the_live_seat(monkeypatch):
    from tools.controller._helpers import append_prerequisites
    from agents.task.loop_types.controller_types import ActionResult
    orch = types.SimpleNamespace(_public_session=False, _correspondent_tainted=True)
    controller = types.SimpleNamespace(orchestrator=orch, _third_party_seat=False,
                                       list_tools=lambda: [], _activated_skills=set(),
                                       _session_skills={})
    seen = {}

    class SM:
        def prerequisites_of(self, sid, **kw):
            seen.update(kw)
            return []
    append_prerequisites(ActionResult(extracted_content="x",
                                      metadata={"skill_loaded": "dca"}), controller, SM(), None)
    assert seen["third_party"] is True


def test_state_delta_index_refresh_asks_the_live_seat(monkeypatch):
    from agents.task.agent.core import state_delta
    seen = {}

    class SM:
        def declared_tool_ids(self, uid):
            return set()

        def get_catalog_skills(self, **kw):
            seen.update(kw)
            return []
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: SM())
    orch = types.SimpleNamespace(user_id=None, _public_session=False,
                                 _correspondent_tainted=True)
    controller = types.SimpleNamespace(
        orchestrator=orch, _third_party_seat=False, _skill_index_stale=True,
        list_tools=lambda: [], absent_tool_ids=lambda ids: set(), _session_skills={})
    agent = types.SimpleNamespace(orchestrator=orch, controller=controller)
    state_delta.skills_delta_part(agent)
    assert seen.get("third_party") is True


# ---- LOW-3 / LOW-4: pins and prerequisites on a third-party seat -------------------

def test_a_money_pin_does_not_load_on_a_third_party_seat():
    got = [m.skill_id for m in SkillManager().get_pinned_skills(
        seeded_skill_ids=["dca"], tool_ids=[], third_party=True)]
    assert "dca" not in got, got
    owner = [m.skill_id for m in SkillManager().get_pinned_skills(
        seeded_skill_ids=["dca"], tool_ids=[])]
    assert owner[0] == "dca"


def test_a_third_party_prerequisite_skips_an_authored_skill(homes):
    sm = _manager_with_authored()
    rules = sm._effective_rules("u1")
    rules = dict(rules)
    rules["lane"] = {"priority": 1, "requires": ["my-programme"]}
    head = sm._entry("lane", rules["lane"], "", "u1", reason="t", trigger_type="seeded")
    owner = sm._with_prerequisites([head], rules, [], "u1")
    assert [m.skill_id for m in owner][1:] == ["my-programme"]
    room = sm._with_prerequisites([head], rules, [], "u1", third_party=True)
    assert [m.skill_id for m in room][1:] == []


# ---- M3: a pinned money playbook brings its safety prerequisites -----------------

def test_a_pinned_money_playbook_brings_its_prerequisites_without_its_tools():
    """Its defi tools may arrive by load_tool later; it never runs without identity."""
    got = SkillManager().get_pinned_skills(seeded_skill_ids=["trade-execution"],
                                           tool_ids=["task"])
    ids = [m.skill_id for m in got]
    assert ids[0] == "trade-execution"
    assert {"token-identity", "pre-trade-check"} <= set(ids), ids


def test_a_prerequisite_over_the_cap_is_logged(tmp_path, caplog):
    rules = {"lane": {"priority": 1, "requires": ["a", "b", "c"]},
             "a": {"priority": 1}, "b": {"priority": 1}, "c": {"priority": 1}}
    write_library(tmp_path, rules)
    sm = SkillManager(skills_dir=tmp_path)
    with caplog.at_level(logging.WARNING):
        got = [m.skill_id for m in sm.get_pinned_skills(seeded_skill_ids=["lane"])]
    assert got == ["lane", "a", "b"]
    assert "c (requires:lane)" in caplog.text


def test_prerequisites_follow_the_seat(tmp_path):
    rules = {"lane": {"priority": 1, "requires": ["owner-step"]},
             "owner-step": {"priority": 1}}
    write_library(tmp_path, rules)
    md = tmp_path / "owner-step" / "SKILL.md"
    md.write_text(md.read_text().replace("metadata:\n", "metadata:\n  polyrob-owner-only: 'true'\n"))
    sm = SkillManager(skills_dir=tmp_path)
    assert [m.skill_id for m in sm.prerequisites_of("lane", user_id=None)] == ["owner-step"]
    assert sm.prerequisites_of("lane", user_id=None, third_party=True) == []


# ---- M4: the pinned-skill wrapper keeps stop rules binding ------------------------

def test_pinned_wrapper_keeps_stop_rules_binding():
    from agents.task.agent.skill_manager import MatchedSkill
    text = SkillManager().format_skills_for_prompt(
        [MatchedSkill("x", 1, ["seeded"], "# X\n\nStop if the route refuses.")])
    assert "stop, refusal and safety rules are binding" in text
    assert "best available alternative" not in text


# ---- M7: a builtin body changed on disk is not served stale ------------------------

def test_a_changed_builtin_body_is_reloaded(tmp_path):
    write_library(tmp_path, {"widget": {"priority": 3}},
                  {"widget": "# Widget\n\nOld procedure body for the widget skill."})
    sm = SkillManager(skills_dir=tmp_path)
    assert "Old procedure" in sm._load_skill_content("widget")
    md = tmp_path / "widget" / "SKILL.md"
    md.write_text(md.read_text().replace("Old procedure", "Fixed procedure")
                  .replace("polyrob-priority: '3'", "polyrob-priority: '2'"))
    import os
    st = md.stat()
    os.utime(md, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert "Fixed procedure" in sm._load_skill_content("widget")
    assert sm.get_skill_rule("widget")["priority"] == 2


# ---- MED-1 / LOW-2: the load_skill fallback on a tainted seat ----------------------

def _tainted_controller(tmp_path, monkeypatch, sm):
    from tests.unit.skills.test_catalog_cap_and_fallback import _bare_controller
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)
    c = _bare_controller()
    c.orchestrator = types.SimpleNamespace(_public_session=False, _correspondent_tainted=True)
    c._third_party_seat = False  # the construction snapshot: an owner seat
    c._register_default_actions()
    return c


@pytest.mark.asyncio
async def test_load_fallback_refuses_owner_only_after_live_taint(tmp_path, monkeypatch):
    from tests.unit.skills.test_catalog_cap_and_fallback import _call_load_skill
    c = _tainted_controller(tmp_path, monkeypatch, SkillManager())
    res = await _call_load_skill(c, "skill-authoring")
    assert res.error, res.extracted_content
    c.orchestrator._correspondent_tainted = False
    ok = await _call_load_skill(c, "skill-authoring")
    assert not ok.error


@pytest.mark.asyncio
async def test_load_fallback_fails_closed_when_the_gate_raises(tmp_path, monkeypatch):
    from tests.unit.skills.test_catalog_cap_and_fallback import _call_load_skill
    sm = SkillManager()

    def boom(*a, **k):
        raise RuntimeError("gate failure")
    monkeypatch.setattr(sm, "may_load_skill", boom)
    c = _tainted_controller(tmp_path, monkeypatch, sm)
    assert (await _call_load_skill(c, "document-writing")).error
    c.orchestrator._correspondent_tainted = False
    assert not (await _call_load_skill(c, "document-writing")).error  # owner: fail-open


@pytest.mark.asyncio
async def test_session_set_skill_is_regated_after_live_taint(tmp_path, monkeypatch):
    """S1 (final check 1.7.4): an owner-built session holds owner-only skills in
    _session_skills; a correspondent taint mid-session must stop their load and
    their resource reads, not only the disk fallback."""
    from agents.task.agent.skill_manager import MatchedSkill
    from tests.unit.skills.test_catalog_cap_and_fallback import _call_load_skill
    c = _tainted_controller(tmp_path, monkeypatch, SkillManager())
    c._session_skills["skill-authoring"] = MatchedSkill(
        skill_id="skill-authoring", priority=0, match_reasons=["catalog"],
        content="# owner-only body")
    res = await _call_load_skill(c, "skill-authoring")
    assert res.error and "owner-only body" not in (res.extracted_content or "")
    action = c.registry.registry.actions["read_skill_resource"]
    rr = await action.function(action.param_model(skill_id="skill-authoring",
                                                  resource_path="REFERENCE.md"),
                               execution_context=None)
    assert rr.error and "not loaded" in rr.error
    c.orchestrator._correspondent_tainted = False
    ok = await _call_load_skill(c, "skill-authoring")
    assert not ok.error and "owner-only body" in (ok.extracted_content or "")
