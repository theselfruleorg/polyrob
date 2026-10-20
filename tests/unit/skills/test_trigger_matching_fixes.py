"""Review 2026-09-29 D2/D8 on the real library: the money gate and the index rank.

081 Q2 deleted the keyword matcher (D3/D7 tested it); a skill loads when a job
or goal pins it, or when the model picks its index line.
"""
import pytest

from core.skills import scopes
from agents.task.agent.skill_manager import SkillManager


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(scopes, "skills_data_home", lambda: tmp_path / "data")
    return SkillManager()


def _ids(matches):
    return [m.skill_id for m in matches]


# ── D2: a declared NON-money tool does not open a money playbook ────────────

def _index(sm, tools):
    return _ids(sm.get_catalog_skills(tool_ids=tools, max_skills=500))


def test_cron_only_session_sees_no_dca_or_exits(sm):
    got = _index(sm, ["cronjob", "task"])
    assert "dca" not in got
    assert "exits" not in got


def test_gate_counts_money_and_read_tools_not_cronjob():
    triggers = {"tool_ids": ["defi_trade", "defi_data", "cronjob"]}
    assert not SkillManager._money_tool_gate_ok(triggers, ["cronjob", "task"])
    assert SkillManager._money_tool_gate_ok(triggers, ["defi_trade"])
    # A read-only data session still gets the screens.
    assert SkillManager._money_tool_gate_ok(triggers, ["defi_data"])
    # No money tool declared: the gate does not apply.
    assert SkillManager._money_tool_gate_ok({"tool_ids": ["cronjob"]}, [])


def test_granted_session_still_sees_dca(sm):
    assert "dca" in _index(sm, ["defi_trade", "defi_data", "cronjob"])


# ── 081 WS-1.1 / Q2: dead trigger kinds stay deleted ───────────────────────

def test_no_rule_declares_a_dead_trigger(sm):
    """081 WS-1.1 + Q2: action-name, keyword and pattern triggers are deleted."""
    sm._ensure_rules_loaded()
    offenders = sorted(
        sid for sid, r in sm.skill_rules.items()
        if set(r.get("triggers") or {}) & {"action_names", "keywords", "task_patterns"})
    assert offenders == []


# ── D8: external skills rank after the library ──────────────────────────────

def test_external_skills_rank_after_builtin_and_authored(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ext = home / ".claude" / "skills" / "aaa-external"
    ext.mkdir(parents=True)
    (ext / "SKILL.md").write_text(
        "---\nname: aaa-external\ndescription: a third-party skill\n---\n# External\n"
        + "body " * 20)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(scopes, "skills_data_home", lambda: tmp_path / "data")
    monkeypatch.setattr(scopes, "user_external_roots",
                        lambda: [home / ".claude" / "skills"])
    mgr = SkillManager()
    catalog = mgr.get_catalog_skills(max_skills=200)
    ext_entry = next(m for m in catalog if m.skill_id == "aaa-external")
    assert ext_entry.priority >= 9
    assert catalog[-1].skill_id == "aaa-external"
    builtin = [m for m in catalog if m.skill_id != "aaa-external"]
    assert all(m.priority < ext_entry.priority for m in builtin)
