"""Progressive disclosure should be able to expose ALL available skills.

Regression for the live S9 gap: a generic session trigger-matched zero skills, so
the agent got NO catalog and could not discover/load any skill. get_catalog_skills
returns every auto-activatable skill (capped) so load_skill can reach them.
"""
from agents.task.agent.skill_manager import SkillManager


def test_get_catalog_skills_returns_all_available_skills():
    sm = SkillManager()
    catalog = sm.get_catalog_skills()
    ids = {s.skill_id for s in catalog}
    # the exact skill the live S9 run could not find
    assert "project-analyzer" in ids
    # only skills with an actual body file are catalogued (can't load_skill a bodyless rule)
    assert len(catalog) >= 3
    for s in catalog:
        assert s.skill_id
        assert s.content  # body preloaded so load_skill serves without disk re-read


def test_get_catalog_skills_is_bounded():
    sm = SkillManager()
    catalog = sm.get_catalog_skills(max_skills=2)
    assert len(catalog) <= 2


def test_polyrob_user_guide_visible_in_catalog_with_its_description():
    """owner-UX P3 T2: the polyrob-user-guide builtin skill must actually be
    reachable via the catalog the model sees, not just present as a rules.json
    entry with a bodiless/orphaned SKILL.md."""
    sm = SkillManager()
    catalog = sm.get_catalog_skills()
    ids = {s.skill_id for s in catalog}
    assert "polyrob-user-guide" in ids

    text = sm.format_skill_catalog(catalog)
    assert 'id="polyrob-user-guide"' in text
    # the index line is the frontmatter `polyrob-when` line (081 WS-4.3)
    assert "The owner asks what you can do, how, or why" in text


# --- 095 W12 (IN-3): the index never cuts an authored skill -------------------

import json

import pytest


@pytest.fixture
def tenant_sm(tmp_path, monkeypatch):
    """The REAL library and packs, with one tenant's skills under tmp."""
    from core.skills import scopes
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(scopes, "user_external_roots", lambda: [])
    monkeypatch.setattr(scopes, "skills_data_home", lambda: tmp_path / "data")
    sm = SkillManager()
    user = sm._user_root("u1")
    user.mkdir(parents=True, exist_ok=True)
    rules = {}
    for i in range(3):
        sid = f"tenant-own-{i}"
        (user / sid).mkdir(parents=True, exist_ok=True)
        (user / sid / "SKILL.md").write_text(f"# Own {i}\n" + "procedure step " * 20)
        rules[sid] = {"priority": 6, "triggers": {}}
    (user / "rules.json").write_text(json.dumps(rules))
    return sm


def test_ratchet_library_packs_and_authored_fit_the_derived_cap(tenant_sm):
    """Ratchet: builtin + pack + authored <= the cap a session uses. A hand-kept
    number ("50 is above the 48-skill library") must never come back."""
    sm = tenant_sm
    sm._ensure_rules_loaded()
    packs = {sid for sid, ds in sm._load_external_skills().items()
             if ds.scope.startswith("pack:")}
    library = set(sm.skill_rules) | packs
    authored = sm._authored_ids("u1")
    assert len(authored) == 3
    assert len(library | authored) <= sm.catalog_cap("u1")
    ids = {s.skill_id for s in sm.get_catalog_skills(user_id="u1", max_skills=None)}
    assert authored <= ids


def test_authored_skill_survives_any_cap(tenant_sm):
    ids = {s.skill_id for s in tenant_sm.get_catalog_skills(user_id="u1", max_skills=1)}
    assert {"tenant-own-0", "tenant-own-1", "tenant-own-2"} <= ids


def test_over_budget_index_degrades_lowest_builtin_band_to_names(tenant_sm, monkeypatch):
    sm = tenant_sm
    catalog = sm.get_catalog_skills(user_id="u1", max_skills=None)
    full = sm.format_skill_catalog(catalog)
    assert "also loadable by id" not in full  # under budget: every line in full
    monkeypatch.setattr(SkillManager, "INDEX_BUDGET_CHARS", len(full) // 2)
    short = sm.format_skill_catalog(catalog)
    assert len(short) < len(full)
    assert "also loadable by id" in short
    for s in catalog:
        assert s.skill_id in short  # names only, never dropped
        if s.authored:
            assert f'- id="{s.skill_id}"' in short  # an authored skill keeps its line
    low = max(s.priority for s in catalog if not s.authored)
    lowest = [s for s in catalog if s.priority == low and not s.authored]
    assert all(f'- id="{s.skill_id}"' not in short for s in lowest)
