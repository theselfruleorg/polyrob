"""Regression tests for PathValidator confinement.

The old implementation used ``realpath().startswith(root)`` — a *string* prefix
check — so a sibling directory sharing the root as a prefix (``/tmp/ws-evil``
vs root ``/tmp/ws``) passed validation. Confinement now delegates to
core.path_safety.is_within_root (commonpath after realpath).
"""
import os

from utils.path_validator import PathValidator, sanitize_filename


def test_sibling_directory_sharing_prefix_is_denied(tmp_path):
    ws = tmp_path / "ws"
    evil = tmp_path / "ws-evil"
    ws.mkdir()
    evil.mkdir()
    (evil / "secret.txt").write_text("x")

    v = PathValidator()
    assert v.is_path_allowed(str(ws / "file.txt"), workspace_dir=str(ws)) is True
    assert v.is_path_allowed(str(evil / "secret.txt"), workspace_dir=str(ws)) is False


def test_traversal_out_of_workspace_is_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    v = PathValidator()
    assert v.is_path_allowed(str(ws / ".." / "outside.txt"), workspace_dir=str(ws)) is False


def test_allowed_paths_list_still_works(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    v = PathValidator([str(allowed)])
    assert v.is_path_allowed(str(allowed / "f.txt")) is True
    assert v.is_path_allowed(str(tmp_path / "allowed-evil" / "f.txt")) is False


def test_no_roots_configured_denies(tmp_path):
    v = PathValidator()
    assert v.is_path_allowed(str(tmp_path / "f.txt")) is False


def test_symlink_escape_is_denied(tmp_path):
    ws = tmp_path / "ws"
    outside = tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    link = ws / "link"
    os.symlink(outside, link)
    v = PathValidator()
    assert v.is_path_allowed(str(link / "f.txt"), workspace_dir=str(ws)) is False


def test_sanitize_filename_basics():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("a b?.txt") == "a_b_.txt"
    assert sanitize_filename("") == "unnamed_file"
