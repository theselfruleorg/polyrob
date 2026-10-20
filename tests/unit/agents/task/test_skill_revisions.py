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


def test_an_unreadable_revision_store_cannot_disable_verification(manager, monkeypatch):
    pending = manager.create_skill("calibration", BODY, user_id="u1")
    assert manager.promote_pending_skill("calibration", user_id="u1",
                                         expected_revision=pending.revision).ok
    from modules.skills import skill_usage

    def _boom(*a, **k):
        raise OSError("database disk image is malformed")

    monkeypatch.setattr(skill_usage.SkillUsageStore, "get_revision", _boom)
    assert SkillManager(skills_dir=manager.skills_dir)._load_skill_content("calibration", "u1") == ""


# ── 081 WS-4.2: the revision lives in skill_usage.db (read-old / write-new) ──

def _sha(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_an_active_write_records_the_revision_in_the_db_not_rules_json(manager, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "false")
    res = manager.create_skill("calibration", BODY, user_id="u1", created_by="user")
    assert res.ok and not res.pending
    from modules.skills.skill_usage import get_skill_usage_store
    assert get_skill_usage_store().get_revision("calibration", "u1") == res.revision
    entry = json.loads((manager.skills_dir / "user_u1" / "rules.json").read_text())["calibration"]
    assert not {"content_sha256", "revision", "created_by"} & set(entry)
    assert entry["auto_activate"] is True and "triggers" not in entry  # 081 Q2: no keywords


def _legacy(manager, sha):
    root = manager.skills_dir / "user_u1"
    (root / "calibration").mkdir(parents=True)
    (root / "calibration" / "SKILL.md").write_text(BODY)
    (root / "rules.json").write_text(json.dumps({"calibration": {
        "auto_activate": True, "priority": 6, "triggers": {"keywords": ["gauge"]},
        "description": "legacy", "created_by": "agent",
        "revision": sha, "content_sha256": sha}}))
    return root


def test_read_old_a_legacy_rules_json_revision_still_verifies(manager):
    _legacy(manager, _sha(BODY))
    assert "gauge" in SkillManager(skills_dir=manager.skills_dir)._load_skill_content("calibration", "u1")


def test_read_old_a_tampered_legacy_skill_is_still_refused(manager):
    _legacy(manager, _sha(UPDATED))
    assert SkillManager(skills_dir=manager.skills_dir)._load_skill_content("calibration", "u1") == ""


def test_write_new_the_next_write_moves_a_legacy_revision_to_the_db(manager, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "false")
    root = _legacy(manager, _sha(BODY))
    res = manager.patch_skill("calibration", user_id="u1", old_string="value",
                              new_string="reading", created_by="user")
    assert res.ok, res.errors
    from modules.skills.skill_usage import get_skill_usage_store
    assert get_skill_usage_store().get_revision("calibration", "u1") == res.revision
    entry = json.loads((root / "rules.json").read_text())["calibration"]
    assert not {"content_sha256", "revision", "created_by"} & set(entry)
    assert "triggers" not in entry  # 081 Q2: the dead keyword triggers leave on the next write
    assert "reading" in SkillManager(skills_dir=manager.skills_dir)._load_skill_content("calibration", "u1")


def test_a_failed_revision_commit_cannot_load_the_new_body(manager, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "false")
    assert manager.create_skill("calibration", BODY, user_id="u1", created_by="user").ok
    from modules.skills import skill_usage

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(skill_usage.SkillUsageStore, "record_revision", _boom)
    assert not manager.create_skill("calibration", UPDATED, user_id="u1", created_by="user").ok
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
