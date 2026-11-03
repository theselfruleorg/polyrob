"""098 MO-17: a skill write that stopped between its files is repaired."""
import pytest

from agents.task.agent.skill_manager import SkillManager
from modules.skills.repair import repair_uncommitted_skills

V1 = "# My Skill\n\nWhen X, do Y. This is a useful reusable procedure with enough text.\n"
V2 = "# My Skill\n\nWhen X, do Z instead. A reusable procedure with enough words in it.\n"


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "false")
    from modules.skills import skill_usage as su
    store = su.SkillUsageStore(str(tmp_path / "skill_usage.db"))
    monkeypatch.setattr(su, "get_skill_usage_store", lambda *a, **k: store)
    m = SkillManager(skills_dir=tmp_path)
    m._store = store
    return m


def _active(sm):
    return sm.skills_dir / "user_u1" / "s" / "SKILL.md"


def test_body_without_revision_rolls_back_to_archive(sm):
    assert sm.create_skill("s", V1, user_id="u1", created_by="user").ok
    committed = sm._store.get_revision("s", "u1")
    # a patch archived V1 and wrote V2's body, then died before the commit
    sm._archive_prior_body("u1", "s", _active(sm))
    _active(sm).write_text(V2)
    res = repair_uncommitted_skills(sm, "u1")
    assert res["rolled_back"] == ["s"]
    assert sm._content_revision(_active(sm).read_text()) == committed


def test_body_without_any_archive_is_quarantined(sm):
    assert sm.create_skill("s", V1, user_id="u1", created_by="user").ok
    _active(sm).write_text(V2)          # no archive of the committed body exists
    for p in (sm.skills_dir / "user_u1" / ".archived").rglob("*"):
        if p.is_file():
            p.unlink()
    res = repair_uncommitted_skills(sm, "u1")
    assert res["quarantined"] == ["s"]
    assert not _active(sm).exists()
    assert (sm.skills_dir / "user_u1" / ".pending" / "s" / "SKILL.md").read_text() == V2


def test_pending_equal_to_active_is_removed(sm):
    assert sm.create_skill("s", V1, user_id="u1", created_by="user").ok
    draft = sm.skills_dir / "user_u1" / ".pending" / "s" / "SKILL.md"
    draft.parent.mkdir(parents=True)
    draft.write_text(V1)                # the promote moved it; the delete never ran
    res = repair_uncommitted_skills(sm, "u1")
    assert res["draft_removed"] == ["s"] and not draft.exists()
    assert _active(sm).read_text() == V1


def test_committed_skill_is_left_alone(sm):
    assert sm.create_skill("s", V1, user_id="u1", created_by="user").ok
    assert not any(repair_uncommitted_skills(sm, "u1").values())
