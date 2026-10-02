"""Security analysis 2026-09-23: create_file_path containment (H09) and
clean_user_id collisions / dot-only ids (low)."""
import os

import pytest

from agents.task.path import PathManager


@pytest.fixture
def pm(tmp_path):
    return PathManager(data_root=str(tmp_path / "data" / "task"))


def test_create_file_path_normal(pm, tmp_path):
    p = pm.create_file_path("sess1", "logs", "a.json", "u1")
    assert p.name == "a.json" and "logs" in p.parts


def test_create_file_path_refuses_traversal(pm):
    with pytest.raises(ValueError, match="escapes"):
        pm.create_file_path("sess1", "logs", "../../../../evil.json", "u1")


def test_create_file_path_refuses_planted_symlink(pm, tmp_path):
    outside = tmp_path / "host.txt"
    outside.write_text("host")
    sub = pm.get_subdir("sess1", "workspace", "u1")
    os.symlink(outside, sub / "tool_result_1.txt")
    with pytest.raises(ValueError, match="escapes"):
        pm.create_file_path("sess1", "workspace", "tool_result_1.txt", "u1")


def test_create_file_path_in_root_symlink_ok(pm):
    sub = pm.get_subdir("sess1", "workspace", "u1")
    (sub / "real.txt").write_text("x")
    os.symlink(sub / "real.txt", sub / "alias.txt")
    assert pm.create_file_path("sess1", "workspace", "alias.txt", "u1").name == "real.txt"


def test_session_metadata_in_session_root(pm):
    p = pm.create_file_path("sess1", ".", ".session_metadata.json", "u1")
    assert p.name == ".session_metadata.json"


@pytest.mark.parametrize("uid", ["u_signal_+4915123", "u_discord_123", "123456789",
                                 "_anonymous_", "local", "0xAbC123", "usr_144639f1e1754bff",
                                 "u_0123abcd"])
def test_legacy_ids_unchanged(pm, uid):
    expected = uid.replace("+", "")
    assert pm.clean_user_id(uid) == expected


def test_stripped_ids_no_longer_collide(pm):
    a, b = pm.clean_user_id("a:b"), pm.clean_user_id("ab")
    assert a != b and b == "ab" and a.startswith("ab-h")
    assert pm.clean_user_id("a/b") != pm.clean_user_id("a:b")
    assert pm.clean_user_id("a:b") == pm.clean_user_id("a:b")  # stable


def test_dot_only_id_not_the_sessions_root(pm):
    uid = pm.clean_user_id(".")
    assert uid.strip(".") and uid.startswith("user_")
    root = pm.get_user_root(".")
    assert root != pm.data_root.resolve()
