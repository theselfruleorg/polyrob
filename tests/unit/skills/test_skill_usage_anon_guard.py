"""Skill provenance/usage must skip the anonymous sentinels, not just empty (findings F6).

Skills converge on the same `is_anonymous` predicate as the memory guard; the action
stays a silent no-op (provenance/metrics are fail-open), but the *test* of anonymity is
now the SSOT predicate so non-empty sentinels no longer get a private named bucket.
"""
from modules.skills.skill_usage import SkillUsageStore


def _store(tmp_path):
    return SkillUsageStore(str(tmp_path / "skill_usage.db"))


def test_provenance_skipped_for_anonymous_sentinel(tmp_path):
    s = _store(tmp_path)
    s.record_provenance("skill-a", "_anonymous_", "background_review")
    assert s.get_provenance("skill-a", "_anonymous_") is None


def test_usage_skipped_for_anonymous_sentinel(tmp_path):
    s = _store(tmp_path)
    s.bump_load("skill-a", "system")
    assert s.get_usage("skill-a", "system").get("load_count", 0) == 0


def test_real_tenant_still_recorded(tmp_path):
    s = _store(tmp_path)
    s.record_provenance("skill-a", "local", "user")
    s.bump_load("skill-a", "local")
    assert s.get_provenance("skill-a", "local") is not None
    assert s.get_usage("skill-a", "local").get("load_count", 0) == 1
