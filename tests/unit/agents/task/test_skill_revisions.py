"""Version-bound writable-skill regression tests (U07)."""
import json

import pytest

from agents.task.agent.skill_manager import SkillManager


BODY = "# Calibration\n\nRead the gauge, record the value, and verify the result.\n"
UPDATED = "# Calibration\n\nRead the gauge, record the corrected value, and verify it.\n"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "true")
    return SkillManager(skills_dir=tmp_path)


def test_pending_skill_has_reviewable_exact_revision(manager):
    result = manager.create_skill("calibration", BODY, user_id="u1")
    assert result.ok and result.pending and len(result.revision) == 64
    pending = manager.list_pending_skills("u1")
    assert pending[0]["revision"] == result.revision
    metadata = json.loads((manager.skills_dir / "user_u1" / ".pending" / "calibration" /
                           "REVISION.json").read_text())
    assert metadata["content_sha256"] == result.revision


def test_stale_patch_cas_cannot_overwrite_newer_pending_body(manager):
    first = manager.create_skill("calibration", BODY, user_id="u1")
    second = manager.patch_skill("calibration", user_id="u1", old_string="gauge",
                                 new_string="sensor", expected_revision=first.revision)
    assert second.ok
    stale = manager.patch_skill("calibration", user_id="u1", old_string="sensor",
                                new_string="meter", expected_revision=first.revision)
    assert not stale.ok and "revision conflict" in stale.errors[0]
    assert manager._read_skill_text(manager.skills_dir / "user_u1" / ".pending" /
                                    "calibration" / "SKILL.md").find("sensor") >= 0


def test_promotion_binds_to_reviewed_pending_bytes(manager):
    first = manager.create_skill("calibration", BODY, user_id="u1")
    changed = manager.patch_skill("calibration", user_id="u1", old_string="gauge",
                                  new_string="sensor", expected_revision=first.revision)
    refused = manager.promote_pending_skill("calibration", user_id="u1",
                                            expected_revision=first.revision)
    assert not refused.ok and "changed after review" in refused.errors[0]
    promoted = manager.promote_pending_skill("calibration", user_id="u1",
                                             expected_revision=changed.revision)
    assert promoted.ok and promoted.revision == changed.revision
    assert "sensor" in manager._read_skill_text(manager.skills_dir / "user_u1" /
                                                  "calibration" / "SKILL.md")


def test_versioned_active_skill_refuses_tampered_body(manager):
    pending = manager.create_skill("calibration", BODY, user_id="u1")
    assert manager.promote_pending_skill("calibration", user_id="u1",
                                         expected_revision=pending.revision).ok
    path = manager.skills_dir / "user_u1" / "calibration" / "SKILL.md"
    path.write_text(UPDATED)
    fresh = SkillManager(skills_dir=manager.skills_dir)
    assert fresh._load_skill_content("calibration", "u1") == ""


def test_legacy_active_skill_without_revision_still_loads(manager):
    root = manager.skills_dir / "user_u1"
    path = root / "legacy" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(BODY)
    (root / "rules.json").write_text(json.dumps({"legacy": {"auto_activate": True}}))
    fresh = SkillManager(skills_dir=manager.skills_dir)
    assert "gauge" in fresh._load_skill_content("legacy", "u1")


def test_failed_rule_commit_cannot_load_new_unverified_active_body(manager, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "false")
    initial = manager.create_skill("calibration", BODY, user_id="u1", created_by="user")
    assert initial.ok
    monkeypatch.setattr(manager, "_upsert_rule", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    failed = manager.create_skill("calibration", UPDATED, user_id="u1", created_by="user")
    assert not failed.ok
    fresh = SkillManager(skills_dir=manager.skills_dir)
    assert fresh._load_skill_content("calibration", "u1") == ""


def test_revision_metadata_cannot_be_malformed_into_a_load(manager):
    root = manager.skills_dir / "user_u1"
    path = root / "calibration" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(BODY)
    (root / "rules.json").write_text(json.dumps({"calibration": {
        "auto_activate": True, "content_sha256": "not-a-hash",
    }}))
    assert SkillManager(skills_dir=manager.skills_dir)._load_skill_content("calibration", "u1") == ""


def test_corrupt_rules_cannot_disable_version_verification(manager):
    pending = manager.create_skill("calibration", BODY, user_id="u1")
    assert manager.promote_pending_skill("calibration", user_id="u1",
                                         expected_revision=pending.revision).ok
    (manager.skills_dir / "user_u1" / "rules.json").write_text("not json")
    assert SkillManager(skills_dir=manager.skills_dir)._load_skill_content("calibration", "u1") == ""


def test_busy_tenant_transaction_fails_closed(manager):
    import fcntl
    import os

    root = manager.skills_dir / "user_u1"
    root.mkdir(parents=True)
    lock = root / ".skill-write.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = manager.create_skill("calibration", BODY, user_id="u1")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert not result.ok and "busy" in result.errors[0]
