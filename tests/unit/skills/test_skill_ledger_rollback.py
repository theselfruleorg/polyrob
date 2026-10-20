"""081 WS-5.2: every skill mutation is one ledger row; rollback restores an
archived revision through the write gate; a consolidation delete names its absorber."""
import pytest

from agents.task.agent.skill_manager import SkillManager
from modules.skills.skill_usage import get_skill_usage_store

V1 = "# Deploy\n\nStep one: build the tree. Step two: ship it with the script.\n"
V2 = "# Deploy\n\nStep one: build the tree. Step two: ship it with deploy_when_idle.\n"


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "false")
    monkeypatch.setenv("SKILL_OVERWRITE_PROTECT", "false")
    return SkillManager(skills_dir=tmp_path)


def _rows(skill_id, uid="u1"):
    return get_skill_usage_store().list_changes(skill_id, uid)


def test_each_mutation_appends_one_row(sm):
    a = sm.create_skill("deploy", V1, user_id="u1", created_by="user")
    b = sm.patch_skill("deploy", user_id="u1", old_string="the script",
                       new_string="deploy_when_idle", created_by="user")
    assert a.ok and b.ok and b.revision != a.revision
    assert sm.delete_skill("deploy", user_id="u1") is True
    rows = _rows("deploy")
    assert [r["action"] for r in rows] == ["delete", "patch", "create"]
    delete, patch, create = rows
    assert (create["before_sha"], create["after_sha"]) == (None, a.revision)
    assert (patch["before_sha"], patch["after_sha"]) == (a.revision, b.revision)
    assert patch["archive_path"].endswith(f"{a.revision}-SKILL.md")
    assert (delete["before_sha"], delete["after_sha"], delete["actor"]) == (b.revision, None, "user")


def test_rollback_restores_the_replaced_revision(sm):
    a = sm.create_skill("deploy", V1, user_id="u1", created_by="user")
    sm.create_skill("deploy", V2, user_id="u1", created_by="user", action="edit")
    res = sm.rollback_skill("deploy", user_id="u1", created_by="user")
    assert res.ok and res.revision == a.revision
    assert (sm.skills_dir / "user_u1" / "deploy" / "SKILL.md").read_text() == V1
    assert _rows("deploy")[0]["action"] == "rollback"


def test_rollback_to_a_named_sha_and_after_delete(sm):
    a = sm.create_skill("deploy", V1, user_id="u1", created_by="user")
    sm.create_skill("deploy", V2, user_id="u1", created_by="user", action="edit")
    assert sm.delete_skill("deploy", user_id="u1")
    res = sm.rollback_skill("deploy", user_id="u1", to_sha=a.revision[:10], created_by="user")
    assert res.ok and (sm.skills_dir / "user_u1" / "deploy" / "SKILL.md").read_text() == V1


def test_rollback_refuses_an_unknown_sha_and_names_the_archive(sm):
    sm.create_skill("deploy", V1, user_id="u1", created_by="user")
    sm.create_skill("deploy", V2, user_id="u1", created_by="user", action="edit")
    res = sm.rollback_skill("deploy", user_id="u1", to_sha="deadbeefdeadbeef")
    assert not res.ok and "is not archived" in res.errors[0]


def test_agent_rollback_of_an_active_skill_is_a_pending_draft(sm, monkeypatch):
    monkeypatch.setenv("SKILL_OVERWRITE_PROTECT", "true")
    sm.create_skill("deploy", V1, user_id="u1", created_by="user")
    sm.create_skill("deploy", V2, user_id="u1", created_by="user", action="edit")
    res = sm.rollback_skill("deploy", user_id="u1", created_by="agent")
    assert res.ok and res.pending
    assert (sm.skills_dir / "user_u1" / "deploy" / "SKILL.md").read_text() == V2


def test_background_delete_must_name_a_live_absorber(sm):
    sm.create_skill("umbrella", V1, user_id="u1", created_by="user")
    sm.create_skill("narrow", V2, user_id="u1", created_by="background_review")  # a draft
    res = sm.archive_skill("narrow", user_id="u1", created_by="background_review")
    assert not res.ok and "absorbed_into" in res.errors[0]
    res = sm.archive_skill("narrow", user_id="u1", created_by="background_review",
                           absorbed_into="missing-skill")
    assert not res.ok
    res = sm.archive_skill("narrow", user_id="u1", created_by="background_review",
                           absorbed_into="umbrella")
    assert res.ok
    assert _rows("narrow")[0]["absorbed_into"] == "umbrella"


def test_curator_age_archive_needs_no_absorber_but_a_foreground_delete_neither(sm):
    sm.create_skill("old", V1, user_id="u1", created_by="agent")
    assert sm.delete_skill("old", user_id="u1", created_by="curator") is False
    assert sm.delete_skill("old", user_id="u1", created_by="curator", aged=True) is True
    assert _rows("old")[0]["action"] == "archive"
    sm.create_skill("mine", V1, user_id="u1", created_by="user")
    assert sm.delete_skill("mine", user_id="u1", created_by="agent") is True


def test_delete_refuses_a_stale_read(sm):
    a = sm.create_skill("deploy", V1, user_id="u1", created_by="user")
    res = sm.archive_skill("deploy", user_id="u1", expected_revision="0" * 64)
    assert not res.ok and res.revision == a.revision
