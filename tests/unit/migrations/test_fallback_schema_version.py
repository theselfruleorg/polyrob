"""The unreadable-dir fallback schema version tracks the newest shipped migration.

It said "1.4.0" while v1_10_0 shipped, so a trimmed layout would have reported a
schema six versions old (structural review 2026-10-10).
"""
from migrations import version_manager as vm


def test_fallback_equals_newest_shipped_migration():
    assert vm._FALLBACK_SCHEMA_VERSION == vm.latest_migration_version()


def test_fallback_used_when_versions_dir_is_empty(tmp_path):
    assert vm.latest_migration_version(tmp_path) == vm._FALLBACK_SCHEMA_VERSION
