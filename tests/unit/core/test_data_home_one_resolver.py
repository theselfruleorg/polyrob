"""2026-10-29 managed user-mode upgrade (1.3.0 → 1.6.0): ONE data home.

The install kept its stores in ``<POLYROB_HOME>/data`` with ``POLYROB_DATA_DIR``
unset. The CLI and the gateway (local mode) resolved ``<POLYROB_HOME>/data``; a
process without ``POLYROB_LOCAL`` (``migrations.migrate``, ``ServerConfig``)
fell back to ``~/.polyrob`` — so the updater's migrate step created a NEW empty
``~/.polyrob/database/bot.db`` and the server config read empty stores. Every
process must resolve the same home, and nothing may open a fresh bot.db while
the real one sits in the other layout.
"""
import sqlite3
from pathlib import Path

import pytest

import core.runtime_paths as rp

_REAL_SERVER_DEFAULT = rp._server_default_data_home
_REAL_LOCAL_DEFAULT = rp._local_default_data_home


def _v130_home(root: Path) -> Path:
    """The tree a 1.3.0 managed user install wrote: everything under <home>/data."""
    home = root / ".polyrob"
    data = home / "data"
    (data / "database").mkdir(parents=True)
    con = sqlite3.connect(data / "database" / "bot.db")
    con.execute("CREATE TABLE v130_marker (id TEXT)")
    con.execute("INSERT INTO v130_marker VALUES ('owner')")
    con.commit()
    con.close()
    con = sqlite3.connect(data / "cron.db")
    con.execute("CREATE TABLE jobs (id TEXT)")
    con.execute("INSERT INTO jobs VALUES ('nightly')")
    con.commit()
    con.close()
    return home


@pytest.fixture
def managed_user_env(tmp_path, monkeypatch):
    """A non-root box: no /var/lib/polyrob, POLYROB_DATA_DIR unset."""
    home = _v130_home(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("POLYROB_HOME", str(home))
    for k in ("POLYROB_DATA_DIR", "DATA_DIR", "DB_PATH", "DATA_ROOT", "POLYROB_LOCAL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(rp, "_SERVER_DATA_HOME", tmp_path / "no-such-dir" / "polyrob")
    monkeypatch.setattr(rp, "_server_default_data_home", _REAL_SERVER_DEFAULT)
    monkeypatch.setattr(rp, "_local_default_data_home", _REAL_LOCAL_DEFAULT)
    monkeypatch.chdir(tmp_path)
    return home


def _rows(db, table):
    con = sqlite3.connect(db)
    try:
        return con.execute(f"SELECT id FROM {table}").fetchall()
    finally:
        con.close()


def test_server_and_local_resolve_the_same_home(managed_user_env):
    home = managed_user_env
    local = rp.resolve_runtime_paths(local=True).data_home
    server = rp.resolve_runtime_paths(local=False).data_home
    assert local == server == (home / "data").resolve()


def test_server_config_and_cli_read_the_same_stores(managed_user_env, monkeypatch):
    home = managed_user_env
    from core.config import BotConfig
    from modules.database.database_manager import resolve_bot_db_path

    monkeypatch.delenv("POLYROB_LOCAL", raising=False)  # the migrate / server shape
    server_cfg = BotConfig()
    monkeypatch.setenv("POLYROB_LOCAL", "1")  # the CLI / gateway shape
    cli_cfg = BotConfig()

    want = (home / "data").resolve()
    assert Path(server_cfg.data_dir).resolve() == want
    assert Path(cli_cfg.data_dir).resolve() == want
    assert resolve_bot_db_path(server_cfg) == want / "database" / "bot.db"
    assert _rows(resolve_bot_db_path(server_cfg), "v130_marker") == [("owner",)]
    assert Path(rp.cron_db_path()).resolve() == want / "cron.db"
    assert Path(rp.cron_db_path(server_cfg.data_dir)).resolve() == want / "cron.db"
    assert _rows(rp.cron_db_path(cli_cfg.data_dir), "jobs") == [("nightly",)]
    # nothing was created in the other layout
    assert not (home / "database").exists()


def test_bot_db_refuses_to_open_fresh_while_the_other_layout_holds_it(tmp_path, monkeypatch):
    """A flat <home>/database/bot.db with the resolved <home>/data one absent
    (or the reverse) is a split, never a fresh start."""
    from types import SimpleNamespace
    from modules.database.database_manager import resolve_bot_db_path

    monkeypatch.delenv("DB_PATH", raising=False)
    home = tmp_path / ".polyrob"
    (home / "database").mkdir(parents=True)
    sqlite3.connect(home / "database" / "bot.db").execute("CREATE TABLE t (x)").connection.close()
    with pytest.raises(RuntimeError, match="POLYROB_DATA_DIR"):
        resolve_bot_db_path(SimpleNamespace(data_dir=str(home / "data")))

    other = tmp_path / "other"
    (other / "data" / "database").mkdir(parents=True)
    sqlite3.connect(other / "data" / "database" / "bot.db").execute("CREATE TABLE t (x)").connection.close()
    with pytest.raises(RuntimeError, match=str(other / "data" / "database" / "bot.db")):
        resolve_bot_db_path(SimpleNamespace(data_dir=str(other)))


def test_bot_db_opens_normally_when_it_exists_or_nothing_does(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from modules.database.database_manager import resolve_bot_db_path

    monkeypatch.delenv("DB_PATH", raising=False)
    fresh = tmp_path / "fresh" / "data"
    assert resolve_bot_db_path(SimpleNamespace(data_dir=str(fresh))) == fresh / "database" / "bot.db"


def test_migrate_upgrade_uses_the_existing_db_not_a_new_one(managed_user_env):
    """The updater's migrate step (the new tree's ``migrations.migrate upgrade``
    in a process WITHOUT POLYROB_LOCAL) must apply to the 1.3.0 bot.db."""
    import os
    import subprocess
    import sys

    home = managed_user_env
    repo = Path(__file__).resolve().parents[3]
    env = {k: v for k, v in os.environ.items()
           if k not in ("POLYROB_DATA_DIR", "DATA_DIR", "DB_PATH", "POLYROB_LOCAL", "DATA_ROOT")}
    env.update(HOME=str(home.parent), POLYROB_HOME=str(home), PYTHONPATH=str(repo))
    r = subprocess.run([sys.executable, "-m", "migrations.migrate", "upgrade"], cwd=str(home.parent),
                       env=env, capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert not (home / "database").exists(), out
    db = home / "data" / "database" / "bot.db"
    con = sqlite3.connect(db)
    try:
        tables = {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert "db_version" in tables or any("version" in t for t in tables), tables
    assert _rows(db, "v130_marker") == [("owner",)]


def test_legacy_tree_converges_without_polyrob_data_dir(tmp_path, monkeypatch):
    """A server home resolved by default (no POLYROB_DATA_DIR) converges its
    1.4.0 ``<home>/data`` tree too — before migrate opens bot.db."""
    from core.config import BotConfig
    from modules.database.database_manager import resolve_bot_db_path

    server_home = tmp_path / "varlib" / "polyrob"
    (server_home / "data" / "database").mkdir(parents=True)
    con = sqlite3.connect(server_home / "data" / "database" / "bot.db")
    con.execute("CREATE TABLE v140_marker (id TEXT)")
    con.execute("INSERT INTO v140_marker VALUES ('owner')")
    con.commit()
    con.close()
    for k in ("POLYROB_DATA_DIR", "DATA_DIR", "DB_PATH", "POLYROB_LOCAL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(rp, "_SERVER_DATA_HOME", server_home)
    monkeypatch.setattr(rp, "_server_default_data_home", _REAL_SERVER_DEFAULT)
    db = resolve_bot_db_path(BotConfig())
    assert db == server_home.resolve() / "database" / "bot.db"
    assert _rows(db, "v140_marker") == [("owner",)]
