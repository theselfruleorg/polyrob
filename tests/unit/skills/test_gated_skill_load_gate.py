"""P1-1 / P1-7 (intelligence-polish plan 2026-07-07): the auto_activate gate must be
real on the LOAD path, gated skills must be discoverable when their tools are loaded,
and external skills must be scanned before entering the pinned catalog.
"""
import json
from pathlib import Path

from agents.task.agent.skill_manager import SkillManager
from tests.support.skill_library import write_library


def _make_manager(tmp_path: Path, rules: dict, bodies: dict) -> SkillManager:
    write_library(tmp_path, rules, bodies)
    return SkillManager(skills_dir=tmp_path)


_RULES = {
    "web-research": {"priority": 5, "auto_activate": True, "triggers": {}},
    "polymarket-trading": {
        "priority": 5, "auto_activate": False,
        "triggers": {"tool_ids": ["polymarket"]},
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


# ---- 067 P0.1: auto_activate money playbooks, catalog == load ----------------

_MONEY_RULES = {
    "web-research": {"priority": 5, "auto_activate": True, "triggers": {}},
    "treasury-trading": {
        "priority": 5, "auto_activate": True,
        "triggers": {"tool_ids": ["defi_trade", "defi_data", "launchpad", "dapp_browser"]},
    },
}
_MONEY_BODIES = {
    "web-research": "# Web research\nDo research.",
    "treasury-trading": "# Treasury\nExecution doctrine.",
}


def test_money_playbook_not_in_session_catalog_without_money_tools(tmp_path):
    sm = _make_manager(tmp_path, _MONEY_RULES, _MONEY_BODIES)
    ids = {m.skill_id for m in sm.get_catalog_skills(tool_ids=["browser"])}
    assert "treasury-trading" not in ids
    assert "web-research" in ids
    assert sm.may_load_skill("treasury-trading", tool_ids=["browser"]) is False


def test_money_playbook_listed_and_loadable_with_a_declared_tool(tmp_path):
    sm = _make_manager(tmp_path, _MONEY_RULES, _MONEY_BODIES)
    # ANY declared tool satisfies the gate (a read-only defi_data session keeps the screens)
    for tools in (["defi_trade"], ["defi_data"]):
        ids = {m.skill_id for m in sm.get_catalog_skills(tool_ids=tools)}
        assert "treasury-trading" in ids
        assert sm.may_load_skill("treasury-trading", tool_ids=tools) is True


def test_money_playbook_listed_in_non_session_listing(tmp_path):
    # tool_ids=None is a listing view (console, export, /skills), not a session
    sm = _make_manager(tmp_path, _MONEY_RULES, _MONEY_BODIES)
    ids = {m.skill_id for m in sm.get_catalog_skills()}
    assert "treasury-trading" in ids
