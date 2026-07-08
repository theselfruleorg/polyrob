"""P1-1 / P1-7 (intelligence-polish plan 2026-07-07): the auto_activate gate must be
real on the LOAD path, gated skills must be discoverable when their tools are loaded,
and external skills must be scanned before entering the pinned catalog.
"""
import json
from pathlib import Path

from agents.task.agent.skill_manager import SkillManager


def _make_manager(tmp_path: Path, rules: dict, bodies: dict) -> SkillManager:
    (tmp_path / "rules.json").write_text(json.dumps(rules))
    for sid, body in bodies.items():
        d = tmp_path / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body)
    return SkillManager(skills_dir=tmp_path)


_RULES = {
    "web-research": {"priority": 5, "auto_activate": True, "triggers": {"keywords": ["research"]}},
    "polymarket-trading": {
        "priority": 5, "auto_activate": False,
        "triggers": {"tool_ids": ["polymarket"], "action_names": ["place_limit_order"]},
    },
}
_BODIES = {
    "web-research": "# Web research\nDo research.",
    "polymarket-trading": "# Polymarket\nTrade playbook.",
}


# ---- may_load_skill (the load gate) ---------------------------------------


def test_gated_skill_not_loadable_without_tools(tmp_path):
    sm = _make_manager(tmp_path, _RULES, _BODIES)
    assert sm.may_load_skill("polymarket-trading", tool_ids=[]) is False
    assert sm.may_load_skill("polymarket-trading", tool_ids=["browser"]) is False


def test_gated_skill_loadable_when_tools_present(tmp_path):
    sm = _make_manager(tmp_path, _RULES, _BODIES)
    assert sm.may_load_skill("polymarket-trading", tool_ids=["polymarket"]) is True


def test_auto_activate_skill_always_loadable(tmp_path):
    sm = _make_manager(tmp_path, _RULES, _BODIES)
    assert sm.may_load_skill("web-research", tool_ids=[]) is True


def test_unknown_skill_id_loadable_other_guards_apply(tmp_path):
    sm = _make_manager(tmp_path, _RULES, _BODIES)
    assert sm.may_load_skill("not-a-real-skill", tool_ids=[]) is True


# ---- catalog surfacing of gated skills ------------------------------------


def test_gated_skill_hidden_from_catalog_without_tools(tmp_path):
    sm = _make_manager(tmp_path, _RULES, _BODIES)
    ids = {m.skill_id for m in sm.get_catalog_skills(tool_ids=[])}
    assert "polymarket-trading" not in ids
    assert "web-research" in ids


def test_gated_skill_in_catalog_when_tools_loaded(tmp_path):
    sm = _make_manager(tmp_path, _RULES, _BODIES)
    ids = {m.skill_id for m in sm.get_catalog_skills(tool_ids=["polymarket"])}
    assert "polymarket-trading" in ids


# ---- external-skill scan (P1-7) -------------------------------------------


def test_external_content_suspicious_helper(tmp_path, monkeypatch):
    sm = _make_manager(tmp_path, {}, {})
    import agents.task.agent.skill_manager as sm_mod

    # scanner flags anything containing SENTINEL
    monkeypatch.setattr(
        "modules.memory.task.threat_scan.is_suspicious",
        lambda text: "SENTINEL" in text,
    )
    assert sm._external_content_suspicious("x", "SENTINEL here", "body") is True
    assert sm._external_content_suspicious("x", "clean desc", "clean body") is False


def test_external_scan_fails_closed_on_raise(tmp_path, monkeypatch):
    sm = _make_manager(tmp_path, {}, {})

    def _boom(_):
        raise RuntimeError("scanner blew up")

    monkeypatch.setattr("modules.memory.task.threat_scan.is_suspicious", _boom)
    # a scanner error must exclude the external skill (fail-closed)
    assert sm._external_content_suspicious("x", "desc", "body") is True


# ---- P2-19: word-boundary keyword matching --------------------------------


def test_p2_19_keyword_word_boundary_no_substring_false_positive(tmp_path):
    """A short keyword must not fire on a substring of an unrelated word."""
    rules = {"trade-safety": {"priority": 1, "auto_activate": True,
                              "triggers": {"keywords": ["sell", "buy", "trade"]}}}
    bodies = {"trade-safety": "# Trade safety\nBe careful."}
    sm = _make_manager(tmp_path, rules, bodies)
    # "counselling" contains "sell", "busy" contains "buy" — must NOT match
    matched = sm.get_skills_for_session(task="I need counselling because I'm busy", tool_ids=[])
    assert not any(m.skill_id == "trade-safety" for m in matched)


def test_p2_19_keyword_matches_real_word(tmp_path):
    rules = {"trade-safety": {"priority": 1, "auto_activate": True,
                              "triggers": {"keywords": ["sell", "buy", "trade"]}}}
    bodies = {"trade-safety": "# Trade safety\nBe careful."}
    sm = _make_manager(tmp_path, rules, bodies)
    matched = sm.get_skills_for_session(task="should I sell my position now", tool_ids=[])
    assert any(m.skill_id == "trade-safety" for m in matched)


def test_p2_19_punctuation_keyword_matches(tmp_path):
    """P2-19 Fusion follow-up: a keyword starting/ending with a non-word char (c++,
    .net) must still match — conditional \\b anchoring, not \\b<kw>\\b (which never
    matched such keywords, a silent false-negative the re.error fallback couldn't catch)."""
    rules = {
        "cpp-help": {"priority": 5, "auto_activate": True, "triggers": {"keywords": ["c++"]}},
        "dotnet-help": {"priority": 5, "auto_activate": True, "triggers": {"keywords": [".net"]}},
    }
    bodies = {"cpp-help": "# C++\nhelp", "dotnet-help": "# .NET\nhelp"}
    sm = _make_manager(tmp_path, rules, bodies)
    m1 = sm.get_skills_for_session(task="how do I use c++ templates", tool_ids=[])
    assert any(x.skill_id == "cpp-help" for x in m1)
    m2 = sm.get_skills_for_session(task="build a .net web api", tool_ids=[])
    assert any(x.skill_id == "dotnet-help" for x in m2)


def test_p2_19_word_boundary_still_holds_for_plain_keywords(tmp_path):
    """Regression guard: the substring false-positive fix still holds ('sell' must not
    fire on 'counselling')."""
    rules = {"trade": {"priority": 1, "auto_activate": True, "triggers": {"keywords": ["sell"]}}}
    bodies = {"trade": "# trade\nx"}
    sm = _make_manager(tmp_path, rules, bodies)
    assert not any(x.skill_id == "trade" for x in sm.get_skills_for_session(task="counselling please", tool_ids=[]))
    assert any(x.skill_id == "trade" for x in sm.get_skills_for_session(task="should I sell", tool_ids=[]))
