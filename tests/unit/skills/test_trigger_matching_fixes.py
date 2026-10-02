"""Review 2026-09-29 D2/D3/D7/D8: skill trigger matching on the real library."""
import time

import pytest

from agents.task.agent import skill_store
from agents.task.agent.skill_manager import SkillManager


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(skill_store, "skills_data_home", lambda: tmp_path / "data")
    return SkillManager()


def _ids(matches):
    return [m.skill_id for m in matches]


# ── D2: a declared NON-money tool does not open a money playbook ────────────

def test_cron_only_session_gets_no_dca_or_exits(sm):
    got = _ids(sm.get_skills_for_session(
        task="dca: buy ETH every week and set exits with a take profit",
        tool_ids=["cronjob", "task"], max_skills=10))
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


def test_granted_session_still_gets_dca(sm):
    got = _ids(sm.get_skills_for_session(
        task="dca: buy ETH every week", tool_ids=["defi_trade", "defi_data", "cronjob"],
        max_skills=10))
    assert "dca" in got


# ── D3: always-registered actions never match a skill on their own ──────────

ALWAYS_ON_ACTIONS = ["preferences", "owner_doc_manage", "agent_status", "done",
                     "load_skill", "x402_wallet_status", "twitter_whoami",
                     "x_browser_x_signup_start", "x_browser_x_login_check"]


def test_buy_token_pnl_gets_treasury_trading_and_its_prereqs(sm):
    got = _ids(sm.get_skills_for_session(
        task="buy token PNL for the buyback",
        tool_ids=["defi_trade", "defi_data", "task"],
        available_actions=ALWAYS_ON_ACTIONS + ["defi_trade_swap"]))
    assert "treasury-trading" in got
    assert "token-identity" in got and "pre-trade-check" in got
    assert "polyrob-user-guide" not in got
    assert "self-deploy" not in got


def test_summarize_a_pdf_gets_document_writing(sm):
    got = _ids(sm.get_skills_for_session(
        task="summarize this pdf", tool_ids=["task"], available_actions=ALWAYS_ON_ACTIONS))
    assert "document-writing" in got
    assert "polyrob-user-guide" not in got
    assert "self-deploy" not in got


def test_no_builtin_rule_matches_on_actions_alone(sm):
    """A rule with no tool_ids and action_names loads on an action match alone
    (skill_manager's action-only branch). Builtin rules must not rely on it:
    the actions they named are registered in every session."""
    sm._ensure_rules_loaded()
    offenders = sorted(
        sid for sid, r in sm.skill_rules.items()
        if not (r.get("triggers") or {}).get("tool_ids")
        and (r.get("triggers") or {}).get("action_names"))
    assert offenders == []


def test_the_owner_questions_still_reach_the_guide_and_self_deploy(sm):
    assert "polyrob-user-guide" in _ids(sm.get_skills_for_session(
        task="what can you do?", tool_ids=["task"]))
    assert "self-deploy" in _ids(sm.get_skills_for_session(
        task="set yourself up and tell me if you are ready", tool_ids=["task"]))


# ── D7: bounded matching on a huge task ─────────────────────────────────────

def test_long_task_matches_in_bounded_time(sm):
    sm._ensure_rules_loaded()
    head = "summarize this report. "
    task = head + ("buy sell swap tokens coins the and of " * 5000)  # ~190 KB
    start = time.monotonic()
    got = _ids(sm.get_skills_for_session(task=task, tool_ids=["task"]))
    assert time.monotonic() - start < 2.0
    assert "document-writing" in got


def test_a_keyword_past_the_head_does_not_match(sm):
    task = ("x " * 3000) + "summarize this"
    assert len(task) > SkillManager.MAX_TRIGGER_TASK_CHARS
    got = _ids(sm.get_skills_for_session(task=task, tool_ids=["task"]))
    assert "document-writing" not in got


# ── D8: external skills rank after the library ──────────────────────────────

def test_external_skills_rank_after_builtin_and_authored(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ext = home / ".claude" / "skills" / "aaa-external"
    ext.mkdir(parents=True)
    (ext / "SKILL.md").write_text(
        "---\nname: aaa-external\ndescription: a third-party skill\n---\n# External\n"
        + "body " * 20)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(skill_store, "skills_data_home", lambda: tmp_path / "data")
    from agents.task.agent import skill_discovery
    monkeypatch.setattr(skill_discovery, "user_external_roots",
                        lambda: [home / ".claude" / "skills"])
    mgr = SkillManager()
    catalog = mgr.get_catalog_skills(max_skills=200)
    ext_entry = next(m for m in catalog if m.skill_id == "aaa-external")
    assert ext_entry.priority >= 9
    assert catalog[-1].skill_id == "aaa-external"
    builtin = [m for m in catalog if m.skill_id != "aaa-external"]
    assert all(m.priority < ext_entry.priority for m in builtin)
