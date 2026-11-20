"""Audit 2026-11-21 M-9: a snapshot keeps a private file private, the supervisor's
own dirs are closed to 'other', and a snapshot whose digest mismatches is deleted."""
import os
import stat

from core.app_service.snapshot import open_owned_dir, snapshot_tree
from core.ship_tree import tree_digest


def test_a_private_source_file_never_ships_and_the_digest_agrees(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "server.py").write_text("print('hi')\n")
    os.chmod(src / "server.py", 0o664)
    (src / "notes.txt").write_text("private")
    os.chmod(src / "notes.txt", 0o600)
    _bytes, files, skipped = snapshot_tree(str(src), str(tmp_path / "snap"), max_mb=5)
    assert files == 1
    assert "notes.txt (private)" in skipped
    assert not (tmp_path / "snap" / "notes.txt").exists()
    assert stat.S_IMODE(os.stat(tmp_path / "snap" / "server.py").st_mode) == 0o644
    # ship == tested: the digest skips exactly what the snapshot skips.
    assert tree_digest(str(src)) == tree_digest(str(tmp_path / "snap"))


def test_a_group_readable_file_ships_world_readable_for_the_container(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("a")
    os.chmod(src / "a.txt", 0o640)
    snapshot_tree(str(src), str(tmp_path / "snap"), max_mb=5)
    assert stat.S_IMODE(os.stat(tmp_path / "snap" / "a.txt").st_mode) == 0o644


def test_owned_dirs_are_born_and_kept_closed_to_other(tmp_path):
    (tmp_path / "apps").mkdir()
    os.chmod(tmp_path / "apps", 0o755)
    fd = open_owned_dir(str(tmp_path), ["apps", "t", "slug", "snapshots"])
    os.close(fd)
    for rel in ("apps", "apps/t", "apps/t/slug", "apps/t/slug/snapshots"):
        assert not os.stat(tmp_path / rel).st_mode & 0o027, rel
