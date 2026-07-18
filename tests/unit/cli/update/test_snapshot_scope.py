"""U2/U9 (2026-07-14 review) — snapshot scope + rollback selection.

A successful `--apply` used to write TWO snapshots: the full pre-update one (DBs +
config + identity) and a newer DB-only pre-migrate one. A bare `--rollback` picked
`latest_complete` → the DB-only one, while the CLI printed "restores your databases,
config, and identity" — silently leaving config/identity at the updated state.

Fixes under test:
- manifests carry ``scope`` ("full" | "db_only"); legacy manifests infer it;
- ``latest_complete`` prefers the newest complete FULL snapshot (falls back to any);
- ``apply_update`` takes exactly ONE (full) snapshot — migrate_guarded reuses it;
- same-timestamp snapshots get distinct dirs (no silent clobber).
"""
import json
import sqlite3

from cli.update.context import UpdateContext
from cli.update.engine import UpdateRunners, apply_update
from cli.update.migrate_guarded import migrate_guarded
from cli.update.snapshot import (
    MANIFEST_NAME, SnapshotManifest, create_snapshot, latest_complete, list_snapshots,
)


def _seed(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
    c.executemany("INSERT INTO t DEFAULT VALUES", [() for _ in range(rows)])
    c.commit()
    c.close()


def _count(path):
    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        c.close()


def _full_snap(tmp_path, ts, cfg_name="a.env"):
    """A full-scope snapshot (has a config item)."""
    home = tmp_path / "home"
    db = home / "memory.db"
    _seed(db, 3)
    cfg = tmp_path / cfg_name
    cfg.write_text("K=1")
    return create_snapshot(
        snapshots_root=tmp_path / "snaps", data_home=home, db_paths=[db],
        config_paths=[cfg], from_version="0.5.0", timestamp=ts)


def _db_snap(tmp_path, ts):
    """A db_only-scope snapshot."""
    home = tmp_path / "home"
    db = home / "memory.db"
    _seed(db, 3)
    return create_snapshot(
        snapshots_root=tmp_path / "snaps", data_home=home, db_paths=[db],
        from_version="0.5.0", timestamp=ts, scope="db_only")


def test_scope_recorded_in_manifest(tmp_path):
    full = _full_snap(tmp_path, "T1")
    db_only = _db_snap(tmp_path, "T2")
    assert full.manifest.scope == "full"          # default
    assert db_only.manifest.scope == "db_only"
    # survives a manifest round-trip
    assert SnapshotManifest.from_dir(full.path).scope == "full"
    assert SnapshotManifest.from_dir(db_only.path).scope == "db_only"


def test_latest_complete_prefers_full_over_newer_db_only(tmp_path):
    _full_snap(tmp_path, "T1")
    _db_snap(tmp_path, "T2")  # newer, but db-only
    picked = latest_complete(tmp_path / "snaps")
    assert picked is not None
    assert picked.manifest.scope == "full"
    assert picked.name.startswith("T1")


def test_latest_complete_falls_back_to_db_only(tmp_path):
    _db_snap(tmp_path, "T1")
    picked = latest_complete(tmp_path / "snaps")
    assert picked is not None
    assert picked.manifest.scope == "db_only"


def test_legacy_manifest_scope_inferred(tmp_path):
    full = _full_snap(tmp_path, "T1")
    db_only = _db_snap(tmp_path, "T2")
    # Strip the scope key to simulate pre-fix manifests.
    for snap in (full, db_only):
        mpath = snap.path / MANIFEST_NAME
        data = json.loads(mpath.read_text())
        data.pop("scope", None)
        mpath.write_text(json.dumps(data))
    # Inference: non-db items present → full; db items only → db_only.
    assert SnapshotManifest.from_dir(full.path).scope == "full"
    assert SnapshotManifest.from_dir(db_only.path).scope == "db_only"
    picked = latest_complete(tmp_path / "snaps")
    assert picked.name.startswith("T1")


def test_same_timestamp_snapshots_do_not_clobber(tmp_path):
    a = _full_snap(tmp_path, "T1")
    b = _db_snap(tmp_path, "T1")  # same timestamp + version → same base dir name
    assert a.path != b.path
    assert len(list_snapshots(tmp_path / "snaps")) == 2
    # the first snapshot's manifest is untouched
    assert SnapshotManifest.from_dir(a.path).scope == "full"


# ---------------------------------------------------------------------------
# U9: one snapshot per apply — migrate_guarded reuses the engine's
# ---------------------------------------------------------------------------

def _ok_runners():
    return UpdateRunners(install=lambda: None, migrate=lambda: None,
                         verify=lambda: None, rollback_code=lambda: None)


def test_apply_update_takes_exactly_one_full_snapshot(tmp_path):
    db = tmp_path / "home" / "memory.db"
    _seed(db, 10)
    ctx = UpdateContext(data_home=tmp_path / "home",
                        snapshots_root=tmp_path / "snaps", db_paths=[db])
    res = apply_update(ctx=ctx, runners=_ok_runners(),
                       from_version="0.5.0", to_version="0.5.1")
    assert res.ok is True
    snaps = list_snapshots(tmp_path / "snaps")
    assert len(snaps) == 1
    assert snaps[0].manifest.scope == "full"


def test_migrate_guarded_reuses_given_snapshot_on_failure(tmp_path):
    home = tmp_path / "home"
    db = home / "memory.db"
    _seed(db, 10)
    snap = create_snapshot(snapshots_root=tmp_path / "snaps", data_home=home,
                           db_paths=[db], from_version="0.5.0", timestamp="T1")

    def bad_migrate():
        _seed(db, 5)  # now 15
        raise RuntimeError("boom")

    res = migrate_guarded(migrate=bad_migrate, db_paths=[db],
                          snapshots_root=tmp_path / "snaps", data_home=home,
                          from_version="0.5.0", snapshot=snap)
    assert res.ok is False and res.restored is True
    assert res.snapshot.path == snap.path
    assert _count(db) == 10                      # restored from the given snapshot
    assert len(list_snapshots(tmp_path / "snaps")) == 1   # no second snapshot created
