"""H09 (security analysis 2026-09-23): descriptor-anchored file-tool I/O."""
import os
import stat

import pytest

from core.security import workspace_io as wio


def test_write_then_read_roundtrip(tmp_path):
    wio.write_text(tmp_path / "a" / "b.txt", tmp_path, "hello")
    assert wio.read_text(tmp_path / "a" / "b.txt", tmp_path) == "hello"


def test_write_uses_random_temp_not_predictable_name(tmp_path, monkeypatch):
    seen = []
    real_open = os.open

    def spy(name, flags, *a, **kw):
        if str(name).endswith(".tmp"):
            seen.append((name, flags))
        return real_open(name, flags, *a, **kw)

    monkeypatch.setattr(os, "open", spy)
    monkeypatch.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) | {spy})
    wio.write_text(tmp_path / "f.txt", tmp_path, "x")
    assert seen, "no temp file opened"
    name, flags = seen[0]
    assert flags & os.O_EXCL and flags & os.O_NOFOLLOW and flags & os.O_CREAT
    assert name != "f.txt.tmp" and len(name) > len(".f.txt..tmp") + 8


def test_planted_symlink_at_target_is_replaced_not_followed(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "out.txt").symlink_to(outside)
    wio.write_text(ws / "out.txt", ws, "agent")
    assert outside.read_text() == "secret"
    assert not (ws / "out.txt").is_symlink()
    assert (ws / "out.txt").read_text() == "agent"


def test_parent_symlink_escape_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "d").symlink_to(outside)
    with pytest.raises(OSError):
        wio.write_text(ws / "d" / "x.txt", ws, "y")
    with pytest.raises(OSError):
        (outside / "r.txt").write_text("s")
        wio.read_text(ws / "d" / "r.txt", ws)
    assert not (outside / "x.txt").exists()


def test_read_refuses_non_regular(tmp_path):
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(OSError):
        wio.read_bytes(tmp_path / "fifo", tmp_path)


def test_in_root_symlink_read_follows_once(tmp_path):
    (tmp_path / "real.md").write_text("doc")
    (tmp_path / "alias.md").symlink_to(tmp_path / "real.md")
    assert wio.read_text(tmp_path / "alias.md", tmp_path) == "doc"


def test_existing_mode_preserved_and_default_not_0600(tmp_path):
    p = tmp_path / "run.sh"
    p.write_text("#!/bin/sh\n")
    os.chmod(p, 0o755)
    wio.write_text(p, tmp_path, "#!/bin/sh\necho hi\n")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o755
    wio.write_text(tmp_path / "new.txt", tmp_path, "x")
    assert stat.S_IMODE(os.stat(tmp_path / "new.txt").st_mode) & 0o044  # group/other read


def test_append_refuses_symlink_final_component(tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text("")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "log.jsonl").symlink_to(outside)
    with pytest.raises(OSError):
        wio.append_bytes(ws / "log.jsonl", ws, b"{}\n")
    assert outside.read_text() == ""


def test_unlink_removes_the_link_not_the_target(tmp_path):
    (tmp_path / "t.txt").write_text("keep")
    (tmp_path / "l.txt").symlink_to(tmp_path / "t.txt")
    wio.unlink(tmp_path / "l.txt", tmp_path)
    assert (tmp_path / "t.txt").read_text() == "keep"
    assert not os.path.lexists(tmp_path / "l.txt")
