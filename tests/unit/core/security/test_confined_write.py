"""Filesystem-boundary and failure tests for instruction-store mutations."""
import os

import pytest

from core.security.confined_write import replace_confined, write_confined_text


def test_atomic_replace_keeps_previous_content_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "SKILL.md"
    path.write_text("previous")

    def fail_replace(*args, **kwargs):
        raise OSError("fixture replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        write_confined_text(path, tmp_path, "new")
    assert path.read_text() == "previous"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["SKILL.md"]


def test_hardlink_alias_is_not_modified(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("original")
    os.link(outside, root / "SKILL.md")
    with pytest.raises(OSError):
        write_confined_text(root / "SKILL.md", root, "new")
    assert outside.read_text() == "original"


def test_link_swap_during_directory_walk_is_refused(tmp_path, monkeypatch):
    root, outside = tmp_path / "store", tmp_path / "outside"
    (root / "tenant").mkdir(parents=True)
    outside.mkdir()
    real_open = os.open

    def swap_then_open(path, flags, *args, **kwargs):
        if path == "tenant":
            (root / "tenant").rename(root / "previous-tenant")
            (root / "tenant").symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, swap_then_open})
    with pytest.raises(OSError):
        write_confined_text(root / "tenant" / "SKILL.md", root, "new")
    assert not list(outside.iterdir())


def test_unsupported_platform_refuses_before_creating_store(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(OSError, match="unavailable"):
        write_confined_text(tmp_path / "store" / "SKILL.md", tmp_path, "new")
    assert not list(tmp_path.iterdir())


def test_archive_move_refuses_linked_destination(tmp_path):
    root, outside = tmp_path / "store", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    source = root / "SKILL.md"
    source.write_text("previous")
    (root / "archive").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        replace_confined(source, root / "archive" / "SKILL.md", root)
    assert source.read_text() == "previous" and not list(outside.iterdir())
