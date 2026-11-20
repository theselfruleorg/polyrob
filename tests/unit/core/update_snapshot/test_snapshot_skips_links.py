"""A snapshot copies no link out of a group-writable dir (review 2026-11-21 #11).

A link planted in identity/ would make a root ``polyrob update`` copy its TARGET, and a
rollback would write those bytes back as a plain file into the shared dir."""
from core.update_snapshot.snapshot import create_snapshot


def test_a_link_in_a_snapshotted_dir_is_not_followed(tmp_path):
    data_home = tmp_path / "data"
    identity = data_home / "identity"
    (identity / "sub").mkdir(parents=True)
    (identity / "self.md").write_text("mine")
    (identity / "sub" / "note.md").write_text("note")
    secret = tmp_path / "root_only.txt"
    secret.write_text("SECRET")
    (identity / "leak.md").symlink_to(secret)
    (identity / "leakdir").symlink_to(tmp_path)

    info = create_snapshot(snapshots_root=tmp_path / "snaps", data_home=data_home,
                           from_version="1.0.0", db_paths=[], dir_paths=[identity],
                           timestamp="T1")
    copied = {p.relative_to(info.path).as_posix() for p in info.path.rglob("*")}
    assert any(p.endswith("identity/self.md") for p in copied)
    assert any(p.endswith("identity/sub/note.md") for p in copied)
    assert not any("leak" in p for p in copied), copied
    assert "SECRET" not in "".join(
        p.read_text() for p in info.path.rglob("*.md") if p.is_file())
