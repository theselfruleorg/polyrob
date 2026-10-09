import json
from pathlib import Path

import pytest

from cli.update.snapshot import create_snapshot, restore_snapshot, MANIFEST_NAME


def snapshot(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cfg = home / ".env"
    cfg.write_text("BEFORE=1")
    snap = create_snapshot(snapshots_root=tmp_path / "snapshots", data_home=home,
                           from_version="1", db_paths=[], config_paths=[cfg])
    cfg.write_text("AFTER=1")
    return home, cfg, snap


@pytest.mark.parametrize("attack", ["outside_target", "source_traversal", "absolute_source", "source_symlink", "source_hardlink"])
def test_hostile_manifest_cannot_read_or_overwrite_outside(tmp_path, attack):
    home, cfg, snap = snapshot(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("KEEP")
    manifest = snap.path / MANIFEST_NAME
    data = json.loads(manifest.read_text())
    row = data["items"][0]
    stored = snap.path / row["stored"]
    if attack == "outside_target":
        row["original"] = str(outside)
    elif attack == "source_traversal":
        row["stored"] = "../../outside"
    elif attack == "absolute_source":
        row["stored"] = str(outside)
    else:
        stored.unlink()
        if attack == "source_symlink":
            stored.symlink_to(outside)
        else:
            stored.hardlink_to(outside)
    manifest.write_text(json.dumps(data))
    with pytest.raises(OSError):
        restore_snapshot(snap.path, data_home=home)
    assert outside.read_text() == "KEEP"
    assert cfg.read_text() == "AFTER=1"


def test_intermediate_target_symlink_is_not_followed(tmp_path):
    home, cfg, snap = snapshot(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "identity").symlink_to(outside, target_is_directory=True)
    manifest = snap.path / MANIFEST_NAME
    data = json.loads(manifest.read_text())
    data["items"][0]["original"] = str(home / "identity" / "root.conf")
    manifest.write_text(json.dumps(data))
    with pytest.raises(OSError):
        restore_snapshot(snap.path, data_home=home)
    assert list(outside.iterdir()) == []
