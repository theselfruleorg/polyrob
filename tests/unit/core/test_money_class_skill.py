"""112 W3: work that DECIDES money is money-class — a pinned builtin/pack skill labelled
``polyrob-money-class: 'true'`` (pay-for-work). A skill the agent authored never counts."""
from pathlib import Path

import pytest

from core.money.classify import is_money_class
from core.skills import catalog, scopes


@pytest.fixture(autouse=True)
def _package_root(monkeypatch):
    monkeypatch.setattr(scopes, "_builtin_root", scopes.package_skills_root)
    catalog._MONEY_CLASS_CACHE.clear()


def test_pinned_pay_for_work_is_money_class():
    assert is_money_class({"skills": ["pay-for-work"]})


def test_an_unlabelled_or_unknown_skill_is_not():
    assert not is_money_class({"skills": ["web-research"]})
    assert not is_money_class({"skills": ["no-such-skill"]})
    assert not is_money_class({})


def test_a_user_scope_skill_marked_money_is_ignored(tmp_path, monkeypatch):
    skill = tmp_path / "user_u1" / "my-payout"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: my-payout\ndescription: 'x'\nmetadata:\n"
        "  polyrob-money-class: 'true'\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(scopes, "skills_data_home", lambda: Path(tmp_path))
    found = scopes.resolve("my-payout", "u1")
    assert found is not None and found.scope == "user"
    assert not is_money_class({"skills": ["my-payout"]})


def test_the_label_parses_into_the_rule():
    rule = catalog.rule_from_frontmatter({"metadata": {"polyrob-money-class": "true"}})
    assert rule["money_class"] is True
    assert "money_class" not in (catalog.rule_from_frontmatter(
        {"metadata": {"polyrob-priority": "3"}}) or {})


def test_the_runners_register_the_probe():
    """The probe must be live before a runner resolves a model (a fresh process)."""
    import inspect
    import agents.task.goals.goal_run as goal_run
    import cron.runner as runner
    for mod in (runner, goal_run):
        assert "import core.skills.catalog" in inspect.getsource(mod)
