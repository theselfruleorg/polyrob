"""T1.3 — snapshot/restore safety net.

The load-bearing test is the WAL round-trip: snapshot a *live* WAL database that still
has an open writer (data sitting in the -wal, not yet checkpointed), mutate it, restore,
and prove the DB is back to its snapshot-time state. This is the no-data-loss guarantee.
"""
import sqlite3
from pathlib import Path

import pytest

from cli.update.snapshot import (
    create_snapshot, is_complete, latest_complete, list_snapshots,
    prune_snapshots, restore_snapshot, DONE_MARKER,
)


def _wal_db(path: Path, rows: int) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    conn.commit()
    return conn


def _count(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def test_snapshot_restore_live_wal_db(tmp_path):
    data_home = tmp_path / "home"
    snaps = tmp_path / "snaps"
    db = data_home / "memory.db"

    # Live DB with 100 committed rows; connection stays OPEN during snapshot,
    # so committed data is in the -wal file, not yet checkpointed into the .db.
    conn = _wal_db(db, 100)
    assert (db.with_name("memory.db-wal")).exists()  # WAL really is in play

    info = create_snapshot(snapshots_root=snaps, data_home=data_home,
                           from_version="0.4.2", db_paths=[db], timestamp="T1")
    assert info.complete and is_complete(info.path)

    # Mutate after the snapshot: 100 -> 150, then close (engine closes before restore).
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"x{i}",) for i in range(50)])
    conn.commit()
    conn.close()
    assert _count(db) == 150

    restore_snapshot(info.path)
    assert _count(db) == 100  # back to snapshot-time state, WAL data included


def test_restore_recreates_deleted_db(tmp_path):
    data_home = tmp_path / "home"
    snaps = tmp_path / "snaps"
    db = data_home / "goals.db"
    _wal_db(db, 7).close()

    info = create_snapshot(snapshots_root=snaps, data_home=data_home,
                           from_version="0.4.2", db_paths=[db], timestamp="T1")
    db.unlink()
    db.with_name("goals.db-wal").unlink(missing_ok=True)
    db.with_name("goals.db-shm").unlink(missing_ok=True)
    assert not db.exists()

    restore_snapshot(info.path)
    assert db.exists() and _count(db) == 7


def test_restore_refuses_without_done_marker(tmp_path):
    data_home = tmp_path / "home"
    snaps = tmp_path / "snaps"
    db = data_home / "cron.db"
    _wal_db(db, 3).close()
    info = create_snapshot(snapshots_root=snaps, data_home=data_home,
                           from_version="0.4.2", db_paths=[db], timestamp="T1")

    (info.path / DONE_MARKER).unlink()  # simulate a torn snapshot
    with pytest.raises(RuntimeError, match="incomplete snapshot"):
        restore_snapshot(info.path)


def test_config_file_and_dir_roundtrip(tmp_path):
    data_home = tmp_path / "home"
    snaps = tmp_path / "snaps"
    env = data_home / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("SECRET=keep-me\n")
    ident = data_home / "identity" / "rob"
    ident.mkdir(parents=True)
    (ident / "SELF.md").write_text("i am rob")

    info = create_snapshot(snapshots_root=snaps, data_home=data_home,
                           from_version="0.4.2", db_paths=[],
                           config_paths=[env], dir_paths=[data_home / "identity"],
                           timestamp="T1")

    env.write_text("SECRET=clobbered\n")
    (ident / "SELF.md").write_text("tampered")
    (ident / "extra.md").write_text("added later")

    restore_snapshot(info.path)
    assert env.read_text() == "SECRET=keep-me\n"
    assert (ident / "SELF.md").read_text() == "i am rob"
    assert not (ident / "extra.md").exists()  # dir restored to snapshot state


def test_torn_snapshot_excluded_from_latest(tmp_path):
    snaps = tmp_path / "snaps"
    data_home = tmp_path / "home"
    db = data_home / "memory.db"
    _wal_db(db, 1).close()

    good = create_snapshot(snapshots_root=snaps, data_home=data_home,
                           from_version="0.4.2", db_paths=[db], timestamp="T1")
    torn = create_snapshot(snapshots_root=snaps, data_home=data_home,
                           from_version="0.4.2", db_paths=[db], timestamp="T2")
    (torn.path / DONE_MARKER).unlink()  # make T2 torn (newer, but incomplete)

    infos = list_snapshots(snaps)
    assert [i.name for i in infos] == [torn.path.name, good.path.name]  # newest first
    assert latest_complete(snaps).path == good.path  # torn skipped


def test_snapshot_with_concurrent_writer_is_consistent(tmp_path):
    # §3.C: a writer keeps committing DURING the snapshot. The SQLite Online-Backup
    # API must yield a crash-consistent copy (no torn read, no error) — the count is
    # some valid point-in-time value, and restoring it round-trips cleanly.
    import threading

    data_home = tmp_path / "home"
    snaps = tmp_path / "snaps"
    db = data_home / "memory.db"
    conn = _wal_db(db, 100)

    stop = threading.Event()

    def writer():
        w = sqlite3.connect(str(db))
        w.execute("PRAGMA journal_mode=WAL")
        i = 0
        while not stop.is_set():
            w.execute("INSERT INTO t (v) VALUES (?)", (f"c{i}",))
            w.commit()
            i += 1
        w.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        info = create_snapshot(snapshots_root=snaps, data_home=data_home,
                               from_version="0.4.2", db_paths=[db], timestamp="T1")
    finally:
        stop.set()
        t.join(5)
    conn.close()

    assert info.complete
    # The snapshot copy is a valid, readable DB (>= the 100 we started with).
    stored = info.path / next(i.stored for i in info.manifest.items)
    assert sqlite3.connect(str(stored)).execute("SELECT COUNT(*) FROM t").fetchone()[0] >= 100
    # And restoring it produces a clean, readable DB.
    restore_snapshot(info.path)
    assert _count(db) >= 100


def test_corrupt_manifest_is_graceful(tmp_path):
    # §3.D: a corrupt manifest.json must not crash listing; the snapshot is surfaced
    # with manifest=None (flagged), not a stack trace.
    from cli.update.snapshot import MANIFEST_NAME

    snaps = tmp_path / "snaps"
    data_home = tmp_path / "home"
    db = data_home / "memory.db"
    _wal_db(db, 1).close()
    info = create_snapshot(snapshots_root=snaps, data_home=data_home,
                           from_version="0.4.2", db_paths=[db], timestamp="T1")
    (info.path / MANIFEST_NAME).write_text("{ this is not json ")

    infos = list_snapshots(snaps)  # must not raise
    assert len(infos) == 1
    assert infos[0].manifest is None
    # restore surfaces the parse error rather than partially restoring.
    with pytest.raises(Exception):
        restore_snapshot(info.path)


def test_snapshot_dir_is_owner_only(tmp_path):
    """M2: the snapshot dir holds raw copies of `.env.production` (MASTER_SEED)
    and `wallet/` (meta.json/audit.jsonl) — chmod 0700 so no other local-box
    user/process can read it off disk, independent of the credential-name guard
    (which stops the AGENT's own file tools, not other OS principals)."""
    import stat

    data_home = tmp_path / "home"
    snaps = tmp_path / "snaps"
    db = data_home / "memory.db"
    _wal_db(db, 1).close()

    info = create_snapshot(snapshots_root=snaps, data_home=data_home,
                           from_version="0.4.2", db_paths=[db], timestamp="T1")

    mode = stat.S_IMODE(info.path.stat().st_mode)
    assert mode == 0o700, f"snapshot dir must be 0700, got {oct(mode)}"


def test_prune_keeps_n_complete(tmp_path):
    snaps = tmp_path / "snaps"
    data_home = tmp_path / "home"
    db = data_home / "memory.db"
    _wal_db(db, 1).close()

    made = [create_snapshot(snapshots_root=snaps, data_home=data_home,
                            from_version="0.4.2", db_paths=[db], timestamp=f"T{i}").path
            for i in range(5)]
    removed = prune_snapshots(snaps, keep=2)
    remaining = {i.path for i in list_snapshots(snaps)}
    assert remaining == {made[4], made[3]}          # two newest kept
    assert set(removed) == {made[0], made[1], made[2]}
