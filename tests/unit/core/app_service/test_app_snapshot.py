"""032 — snapshot_tree copies the tested tree and refuses what must never ship."""
import os

import pytest

from core.app_service.snapshot import SnapshotTooLarge, snapshot_tree


def _tree(tmp_path):
    src = tmp_path / "proj"
    (src / "sub").mkdir(parents=True)
    (src / ".git").mkdir()
    (src / "__pycache__").mkdir()
    (src / "server.py").write_text("print('hi')\n")
    (src / "sub" / "x.txt").write_text("x")
    (src / ".git" / "HEAD").write_text("ref")
    (src / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    (src / ".env").write_text("SECRET=1\n")
    (src / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    os.symlink("/etc/hostname", src / "link")
    os.symlink(str(tmp_path), src / "sub" / "dirlink")
    return src


def test_copies_files_and_refuses_symlinks_caches_credentials(tmp_path):
    src = _tree(tmp_path)
    dst = tmp_path / "snap"
    total, files, skipped = snapshot_tree(str(src), str(dst), max_mb=10)
    assert files == 2 and total == len("print('hi')\n") + 1
    assert (dst / "server.py").read_text() == "print('hi')\n"
    assert (dst / "sub" / "x.txt").exists()
    assert not (dst / ".env").exists() and not (dst / "id_rsa").exists()
    assert not (dst / ".git").exists() and not (dst / "__pycache__").exists()
    assert not (dst / "link").exists() and not (dst / "sub" / "dirlink").exists()
    joined = "\n".join(skipped)
    assert ".env (credential)" in joined and "id_rsa (credential)" in joined
    assert "link (symlink)" in joined and ".git/" in joined


def test_size_cap_leaves_nothing_behind(tmp_path):
    src = tmp_path / "big"
    src.mkdir()
    (src / "blob.bin").write_bytes(b"\x00" * (2 * 1024 * 1024))
    dst = tmp_path / "snap"
    with pytest.raises(SnapshotTooLarge):
        snapshot_tree(str(src), str(dst), max_mb=1)
    assert not dst.exists()


def test_replaces_a_previous_snapshot(tmp_path):
    src = tmp_path / "p"
    src.mkdir()
    (src / "a").write_text("a")
    dst = tmp_path / "snap"
    dst.mkdir()
    (dst / "stale").write_text("old")
    snapshot_tree(str(src), str(dst), max_mb=1)
    assert (dst / "a").exists() and not (dst / "stale").exists()
    with pytest.raises(ValueError):
        snapshot_tree(str(tmp_path / "missing"), str(dst), max_mb=1)


def test_symlink_swapped_after_the_check_is_refused_not_followed(tmp_path, monkeypatch):
    """TOCTOU: the walk saw a regular file, the path is a symlink by the time we
    copy. Opening O_NOFOLLOW fails instead of following it out of the tree."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "server.py").write_text("print('hi')\n")
    secret = tmp_path / "host-secret"
    secret.write_text("HOST SECRET VALUE\n")
    os.symlink(str(secret), src / "data.bin")
    real_islink = os.path.islink
    monkeypatch.setattr(os.path, "islink",
                        lambda p: False if str(p).endswith("data.bin") else real_islink(p))
    dst = tmp_path / "snap"
    total, files, skipped = snapshot_tree(str(src), str(dst), max_mb=10)
    assert files == 1 and total == len("print('hi')\n")
    assert not (dst / "data.bin").exists()
    assert "data.bin (symlink)" in "\n".join(skipped)
    assert "HOST SECRET" not in "".join(
        (dst / p).read_text() for p in os.listdir(dst))


def test_snapshot_preserves_the_executable_bit(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    script = src / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)
    dst = tmp_path / "snap"
    snapshot_tree(str(src), str(dst), max_mb=10)
    assert os.stat(dst / "run.sh").st_mode & 0o111


def test_digest_describes_exactly_the_shipped_tree(tmp_path):
    """Finding 4: one exclusion policy. The recorded "tested" digest must be the
    digest of the bytes that reach /app."""
    from tools.hf_deploy.digest import compute_workspace_digest
    src = tmp_path / "proj"
    (src / "sub").mkdir(parents=True)
    (src / "node_modules" / "left-pad").mkdir(parents=True)
    (src / "coding_snapshots").mkdir()
    (src / ".git").mkdir()
    (src / ".venv").mkdir()
    (src / "server.py").write_text("print('hi')\n")
    (src / "sub" / "x.txt").write_text("x")
    (src / "node_modules" / "left-pad" / "index.js").write_text("module.exports=1\n")
    (src / "coding_snapshots" / "old.py").write_text("stale")
    (src / ".git" / "HEAD").write_text("ref")
    (src / ".venv" / "pyvenv.cfg").write_text("home=/usr")
    (src / ".env").write_text("SECRET=1\n")
    os.symlink("/etc/hostname", src / "link")
    dst = tmp_path / "snap"
    snapshot_tree(str(src), str(dst), max_mb=10)
    assert compute_workspace_digest(str(src)) == compute_workspace_digest(str(dst))
    # and the shipped tree is what the container needs: vendored deps ship
    assert (dst / "node_modules" / "left-pad" / "index.js").exists()
    assert not (dst / "coding_snapshots").exists() and not (dst / ".venv").exists()


def test_a_change_under_node_modules_moves_the_digest(tmp_path):
    """It ships, so it must be hashed — otherwise a payload dropped there after
    the last green run_tests ships under an unchanged 'tested' digest."""
    from tools.hf_deploy.digest import compute_workspace_digest
    src = tmp_path / "proj"
    (src / "node_modules").mkdir(parents=True)
    (src / "server.py").write_text("print('hi')\n")
    (src / "node_modules" / "dep.js").write_text("clean\n")
    before = compute_workspace_digest(str(src))
    (src / "node_modules" / "dep.js").write_text("exfiltrate()\n")
    assert compute_workspace_digest(str(src)) != before
