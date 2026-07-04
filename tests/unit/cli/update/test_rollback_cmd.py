"""T1.3 (command wiring) — `polyrob update --rollback / --list-snapshots`."""
import sqlite3
from pathlib import Path

from click.testing import CliRunner

import cli.commands.update as up
from cli.commands.update import EXIT_ERROR, EXIT_UP_TO_DATE, update_cmd
from cli.update.context import UpdateContext
from cli.update.snapshot import create_snapshot


def _seed_db(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute("DROP TABLE IF EXISTS t")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    c.executemany("INSERT INTO t DEFAULT VALUES", [() for _ in range(rows)])
    c.commit()
    c.close()


def _count(path: Path) -> int:
    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        c.close()


def _wire_ctx(monkeypatch, data_home: Path):
    uctx = UpdateContext(data_home=data_home, snapshots_root=data_home / "snapshots")
    monkeypatch.setattr(up, "resolve_update_context", lambda *a, **k: uctx)
    return uctx


def test_list_snapshots_empty(monkeypatch, tmp_path):
    _wire_ctx(monkeypatch, tmp_path / "home")
    res = CliRunner().invoke(update_cmd, ["--list-snapshots"])
    assert res.exit_code == EXIT_UP_TO_DATE
    assert "No snapshots yet" in res.output


def test_rollback_restores_latest(monkeypatch, tmp_path):
    home = tmp_path / "home"
    uctx = _wire_ctx(monkeypatch, home)
    db = home / "memory.db"
    _seed_db(db, 10)
    create_snapshot(snapshots_root=uctx.snapshots_root, data_home=home,
                    from_version="0.4.2", db_paths=[db], timestamp="T1")

    # Change the live DB after the snapshot.
    _seed_db(db, 99)
    assert _count(db) == 99

    res = CliRunner().invoke(update_cmd, ["--rollback", "--yes"])
    assert res.exit_code == EXIT_UP_TO_DATE, res.output
    assert "Restored" in res.output
    assert _count(db) == 10  # rolled back to snapshot state


def test_rollback_named_missing_errors(monkeypatch, tmp_path):
    _wire_ctx(monkeypatch, tmp_path / "home")
    res = CliRunner().invoke(update_cmd, ["--rollback", "--snapshot", "nope", "--yes"])
    assert res.exit_code == EXIT_ERROR
    assert "No complete snapshot" in res.output


def test_rollback_no_snapshot_errors(monkeypatch, tmp_path):
    _wire_ctx(monkeypatch, tmp_path / "home")
    res = CliRunner().invoke(update_cmd, ["--rollback", "--yes"])
    assert res.exit_code == EXIT_ERROR
    assert "No complete snapshot" in res.output


def test_rollback_confirmation_abort(monkeypatch, tmp_path):
    home = tmp_path / "home"
    uctx = _wire_ctx(monkeypatch, home)
    db = home / "memory.db"
    _seed_db(db, 5)
    create_snapshot(snapshots_root=uctx.snapshots_root, data_home=home,
                    from_version="0.4.2", db_paths=[db], timestamp="T1")
    _seed_db(db, 42)

    res = CliRunner().invoke(update_cmd, ["--rollback"], input="n\n")
    assert res.exit_code == EXIT_UP_TO_DATE
    assert "Aborted" in res.output
    assert _count(db) == 42  # untouched — user declined


def test_rollback_refuses_while_db_in_use(monkeypatch, tmp_path):
    home = tmp_path / "home"
    uctx = _wire_ctx(monkeypatch, home)
    db = home / "memory.db"
    _seed_db(db, 10)
    create_snapshot(snapshots_root=uctx.snapshots_root, data_home=home,
                    from_version="0.4.2", db_paths=[db], timestamp="T1")
    _seed_db(db, 99)

    holder = sqlite3.connect(str(db))
    try:
        holder.execute("PRAGMA busy_timeout=0")
        holder.execute("BEGIN IMMEDIATE")  # a live writer holds the DB
        res = CliRunner().invoke(update_cmd, ["--rollback", "--yes"])
        assert res.exit_code == EXIT_ERROR, res.output
        assert "in use" in res.output.lower()
        assert "--force" in res.output
        # DB untouched — restore was refused.
        assert _count(db) == 99
    finally:
        holder.rollback()
        holder.close()


def test_rollback_force_overrides_in_use(monkeypatch, tmp_path):
    home = tmp_path / "home"
    uctx = _wire_ctx(monkeypatch, home)
    db = home / "memory.db"
    _seed_db(db, 10)
    create_snapshot(snapshots_root=uctx.snapshots_root, data_home=home,
                    from_version="0.4.2", db_paths=[db], timestamp="T1")
    _seed_db(db, 99)

    holder = sqlite3.connect(str(db))
    holder.execute("PRAGMA busy_timeout=0")
    holder.execute("BEGIN IMMEDIATE")
    try:
        res = CliRunner().invoke(update_cmd, ["--rollback", "--yes", "--force"])
        assert res.exit_code == EXIT_UP_TO_DATE, res.output
        assert "Restored" in res.output
    finally:
        holder.rollback()
        holder.close()
    assert _count(db) == 10  # forced restore went through


def test_list_snapshots_json(monkeypatch, tmp_path):
    import json
    home = tmp_path / "home"
    uctx = _wire_ctx(monkeypatch, home)
    db = home / "memory.db"
    _seed_db(db, 1)
    create_snapshot(snapshots_root=uctx.snapshots_root, data_home=home,
                    from_version="0.4.2", db_paths=[db], timestamp="T1")
    res = CliRunner().invoke(update_cmd, ["--list-snapshots", "--json"])
    data = json.loads(res.output)
    assert len(data) == 1 and data[0]["complete"] is True
    assert data[0]["from_version"] == "0.4.2"
