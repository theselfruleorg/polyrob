"""Snapshot + restore — the atomicity anchor for `polyrob update`.

Before any mutating step, the update engine takes a **complete, crash-consistent**
snapshot of the user's state: every SQLite DB (via the WAL-safe SQLite Online-Backup
API, *not* ``cp`` — a live WAL DB copied with ``cp`` can be torn), plus config files
(``.env``) and directory trees (``identity/``). A ``DONE`` marker is written last; a
snapshot without it is a torn snapshot and is never trusted for restore.

Restore is the sanctioned rollback/downgrade path: it puts every captured item back at
its original absolute path using an atomic ``os.replace``, and clears stale ``-wal``/
``-shm`` sidecars so the restored DB can't be corrupted by a leftover WAL. It refuses a
snapshot lacking ``DONE``.

The DBs MUST be closed during restore (the engine guarantees this by stopping/refusing
while a server or REPL holds them) — file-level restore is correct precisely because the
code swap never touches data-home (``core/runtime_paths`` keeps them disjoint).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from core.db_manifest import all_sqlite_dbs

MANIFEST_NAME = "manifest.json"
DONE_MARKER = "DONE"
_DB_SUBDIR = "db"
_FILE_SUBDIR = "config"
_DIR_SUBDIR = "dirs"


@dataclass
class SnapshotItem:
    kind: str          # "db" | "file" | "dir"
    original: str      # absolute source path
    stored: str        # path relative to the snapshot dir


@dataclass
class SnapshotManifest:
    from_version: str
    to_version: str
    method: str
    created_at: str
    git_sha: Optional[str]
    items: List[SnapshotItem] = field(default_factory=list)
    label: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps({
            "from_version": self.from_version,
            "to_version": self.to_version,
            "method": self.method,
            "created_at": self.created_at,
            "git_sha": self.git_sha,
            "label": self.label,
            "items": [vars(i) for i in self.items],
        }, indent=2)

    @classmethod
    def from_dir(cls, snapshot_dir: Path) -> "SnapshotManifest":
        data = json.loads((snapshot_dir / MANIFEST_NAME).read_text())
        return cls(
            from_version=data.get("from_version", ""),
            to_version=data.get("to_version", ""),
            method=data.get("method", ""),
            created_at=data.get("created_at", ""),
            git_sha=data.get("git_sha"),
            label=data.get("label"),
            items=[SnapshotItem(**i) for i in data.get("items", [])],
        )


@dataclass
class SnapshotInfo:
    path: Path
    manifest: Optional[SnapshotManifest]
    complete: bool  # DONE marker present

    @property
    def name(self) -> str:
        return self.path.name


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sqlite_backup(src: Path, dst: Path) -> None:
    """WAL-safe copy of a live SQLite DB via the Online-Backup API."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _clear_wal_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        try:
            side.unlink()
        except FileNotFoundError:
            pass


def create_snapshot(
    *,
    snapshots_root: Path,
    data_home: Path,
    from_version: str,
    to_version: str = "",
    method: str = "",
    git_sha: Optional[str] = None,
    db_paths: Optional[List[Path]] = None,
    config_paths: Optional[List[Path]] = None,
    dir_paths: Optional[List[Path]] = None,
    timestamp: Optional[str] = None,
    label: Optional[str] = None,
) -> SnapshotInfo:
    """Create a crash-consistent snapshot; the ``DONE`` marker is written last."""
    snapshots_root = Path(snapshots_root)
    data_home = Path(data_home)
    ts = timestamp or _now_stamp()
    snap_dir = snapshots_root / f"{ts}_{from_version or 'unknown'}"
    snap_dir.mkdir(parents=True, exist_ok=True)

    if db_paths is None:
        db_paths = all_sqlite_dbs(data_home)
    config_paths = config_paths or []
    dir_paths = dir_paths or []

    items: List[SnapshotItem] = []

    for i, src in enumerate(db_paths):
        src = Path(src)
        if not src.is_file():
            continue
        rel = f"{_DB_SUBDIR}/{i:02d}_{src.name}"
        _sqlite_backup(src, snap_dir / rel)
        items.append(SnapshotItem("db", str(src.resolve()), rel))

    for i, src in enumerate(config_paths):
        src = Path(src)
        if not src.is_file():
            continue
        rel = f"{_FILE_SUBDIR}/{i:02d}_{src.name}"
        (snap_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, snap_dir / rel)
        items.append(SnapshotItem("file", str(src.resolve()), rel))

    for i, src in enumerate(dir_paths):
        src = Path(src)
        if not src.is_dir():
            continue
        rel = f"{_DIR_SUBDIR}/{i:02d}_{src.name}"
        shutil.copytree(src, snap_dir / rel)
        items.append(SnapshotItem("dir", str(src.resolve()), rel))

    manifest = SnapshotManifest(
        from_version=from_version, to_version=to_version, method=method,
        created_at=ts, git_sha=git_sha, items=items, label=label,
    )
    (snap_dir / MANIFEST_NAME).write_text(manifest.to_json())

    # fsync the tree, THEN write DONE last so a torn snapshot is detectable.
    _fsync_dir(snap_dir)
    (snap_dir / DONE_MARKER).write_text(ts)
    return SnapshotInfo(snap_dir, manifest, complete=True)


def _fsync_dir(path: Path) -> None:
    try:
        for f in path.rglob("*"):
            if f.is_file():
                fd = os.open(str(f), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
    except OSError:
        pass  # fsync is best-effort; correctness rests on the DONE marker ordering


def is_complete(snapshot_dir: Path) -> bool:
    return (Path(snapshot_dir) / DONE_MARKER).exists()


def restore_snapshot(snapshot_dir: Path) -> SnapshotManifest:
    """Restore every captured item to its original path. Refuses a torn snapshot."""
    snapshot_dir = Path(snapshot_dir)
    if not is_complete(snapshot_dir):
        raise RuntimeError(
            f"refusing to restore incomplete snapshot (no {DONE_MARKER} marker): {snapshot_dir}")
    manifest = SnapshotManifest.from_dir(snapshot_dir)

    for item in manifest.items:
        stored = snapshot_dir / item.stored
        target = Path(item.original)
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.kind == "db":
            tmp = target.with_name(target.name + ".restore.tmp")
            shutil.copy2(stored, tmp)
            os.replace(tmp, target)          # atomic swap of the closed DB file
            _clear_wal_sidecars(target)      # drop stale WAL so it isn't replayed
        elif item.kind == "file":
            tmp = target.with_name(target.name + ".restore.tmp")
            shutil.copy2(stored, tmp)
            os.replace(tmp, target)
        elif item.kind == "dir":
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(stored, target)
    return manifest


def list_snapshots(snapshots_root: Path) -> List[SnapshotInfo]:
    """All snapshots, newest first. Incomplete (torn) ones are flagged, not hidden."""
    root = Path(snapshots_root)
    if not root.is_dir():
        return []
    infos: List[SnapshotInfo] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        manifest = None
        if (d / MANIFEST_NAME).exists():
            try:
                manifest = SnapshotManifest.from_dir(d)
            except Exception:
                manifest = None
        infos.append(SnapshotInfo(d, manifest, complete=is_complete(d)))
    infos.sort(key=lambda s: s.path.name, reverse=True)
    return infos


def latest_complete(snapshots_root: Path) -> Optional[SnapshotInfo]:
    for info in list_snapshots(snapshots_root):
        if info.complete:
            return info
    return None


def prune_snapshots(snapshots_root: Path, keep: int = 3) -> List[Path]:
    """Keep the ``keep`` most-recent COMPLETE snapshots; delete older ones + torn ones.

    Returns the list of removed directories. A brand-new torn snapshot (the one being
    written) is never pruned here because pruning runs only after a successful commit.
    """
    infos = list_snapshots(snapshots_root)
    complete = [i for i in infos if i.complete]
    keep_set = {i.path for i in complete[:max(0, keep)]}
    removed: List[Path] = []
    for info in infos:
        if info.path in keep_set:
            continue
        shutil.rmtree(info.path, ignore_errors=True)
        removed.append(info.path)
    return removed
