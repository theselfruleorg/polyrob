"""T0.1 — schema-version honesty.

`DatabaseVersionManager.CURRENT_VERSION` must reflect the highest shipped migration
in `migrations/versions/`, not a hand-typed constant that drifts. The update flow
reports the schema axis separately from the app version, so this value has to be
truthful for `migrate.py status` and the `polyrob update` verify step to mean anything.
"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VERSIONS_DIR = _REPO_ROOT / "migrations" / "versions"


def _latest_migration_version_from_files() -> str:
    versions: list[tuple[int, int, int]] = []
    for p in _VERSIONS_DIR.glob("v*.py"):
        m = re.match(r"v(\d+)_(\d+)_(\d+)_", p.name)
        if m:
            versions.append(tuple(int(x) for x in m.groups()))  # type: ignore[arg-type]
    assert versions, f"no migration files found in {_VERSIONS_DIR}"
    return ".".join(str(x) for x in max(versions))


def test_current_version_matches_latest_migration():
    from migrations.version_manager import DatabaseVersionManager

    assert DatabaseVersionManager.CURRENT_VERSION == _latest_migration_version_from_files()


def test_latest_migration_version_helper_agrees():
    """The production helper must agree with an independent scan of the files."""
    from migrations.version_manager import latest_migration_version

    assert latest_migration_version() == _latest_migration_version_from_files()


def test_status_reads_the_db_upgrade_writes_under_a_data_home(monkeypatch, tmp_path):
    """`upgrade` anchors bot.db at $POLYROB_DATA_DIR/database (BotConfig's
    data_dir IS the data home, 080 WS-1); `status` must read that file, not report "needs baseline"
    right after a successful upgrade — and it reads the newest row by id."""
    import sqlite3
    from migrations import migrate

    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    db = tmp_path / "database" / "bot.db"
    assert migrate._status_db_path() == db.resolve()  # nothing exists yet
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE schema_versions (id INTEGER PRIMARY KEY, version TEXT,"
                    " description TEXT, applied_at TEXT, execution_time_ms INTEGER)")
        # same 1 s timestamp: applied_at order is arbitrary, id order is not
        con.executemany("INSERT INTO schema_versions (version, description, applied_at,"
                        " execution_time_ms) VALUES (?, '', '2026-10-02 00:00:00', 0)",
                        [("1.0.0",), ("9.9.9",)])
    assert migrate._status_db_path() == db.resolve()
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT version FROM schema_versions ORDER BY id ASC").fetchall()
    assert rows[-1][0] == "9.9.9"


def test_status_and_upgrade_open_the_same_bot_db_with_no_data_dir_set(monkeypatch, tmp_path):
    """With POLYROB_DATA_DIR and DB_PATH unset, `status` read the local rule's
    <home>/data/database/bot.db while `upgrade` (BotConfig) wrote
    <effective data home>/database/bot.db."""
    from core import runtime_paths
    from migrations import migrate

    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.setattr(runtime_paths, "effective_data_home", lambda: tmp_path / "server")
    monkeypatch.setattr(runtime_paths, "resolve_data_home", lambda: tmp_path / "local")
    assert migrate._status_db_path() == (tmp_path / "server" / "database" / "bot.db").resolve()

