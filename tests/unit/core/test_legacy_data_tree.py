"""A 1.4.0 server data home (``<POLYROB_DATA_DIR>/data/...``) converges onto the
one-data-dir layout at config construction: bot.db and its rows are found,
nothing is merged, overwritten or deleted."""
import sqlite3
import threading
from pathlib import Path

import pytest

from core.legacy_data_tree import converge_legacy_data_tree, legacy_tree_doctor_line


def _v140_home(root: Path) -> Path:
    """The tree a 1.4.0 server wrote: config.data_dir = <home>/data."""
    home = root / "home"
    legacy = home / "data"
    (legacy / "database").mkdir(parents=True)
    con = sqlite3.connect(legacy / "database" / "bot.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE user_profiles (id TEXT)")
    con.execute("INSERT INTO user_profiles VALUES ('owner')")
    con.commit()
    con.close()
    (legacy / "database" / "bot.db-wal").write_bytes(b"")
    (legacy / "characters").mkdir()
    (legacy / "characters" / "rob.json").write_text("{}")
    sqlite3.connect(legacy / "artifacts.db").close()
    (legacy / "artifacts.db-wal").write_bytes(b"")
    sqlite3.connect(home / "goals.db").close()  # a 1.4.0 store already at <home>
    return home


@pytest.fixture
def server_env(monkeypatch):
    def _set(home: Path):
        monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
        monkeypatch.delenv("DATA_DIR", raising=False)
        monkeypatch.delenv("DB_PATH", raising=False)
        monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    return _set


def _rows(db: Path):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT id FROM user_profiles").fetchall()
    finally:
        con.close()


def test_head_config_opens_the_v140_bot_db_with_its_rows(tmp_path, server_env):
    home = _v140_home(tmp_path)
    server_env(home)
    from core.config import BotConfig
    from modules.database.database_manager import resolve_bot_db_path
    cfg = BotConfig()
    db = resolve_bot_db_path(cfg)
    assert db == home.resolve() / "database" / "bot.db"
    assert (home / "database" / "bot.db-wal").exists()  # siblings moved together
    assert _rows(db) == [("owner",)]
    assert (home / "characters" / "rob.json").is_file()
    assert (home / "artifacts.db").is_file() and (home / "artifacts.db-wal").exists()
    assert not (home / "data" / "database").exists()
    assert "moved" in (legacy_tree_doctor_line(home) or "moved")


def test_a_colliding_name_stays_put_and_is_never_overwritten(tmp_path):
    home = _v140_home(tmp_path)
    (home / "data" / "goals.db").write_bytes(b"legacy-goals")
    before = (home / "goals.db").read_bytes()
    moved = converge_legacy_data_tree(home)
    assert "database/" in moved and "goals.db" not in moved
    assert (home / "goals.db").read_bytes() == before
    assert (home / "data" / "goals.db").read_bytes() == b"legacy-goals"
    line = legacy_tree_doctor_line(home)
    assert line and "goals.db" in line and "compare by hand" in line


def test_a_bot_db_collision_refuses_to_start_and_moves_nothing(tmp_path, server_env):
    home = _v140_home(tmp_path)
    (home / "database").mkdir()
    (home / "database" / "bot.db-wal").write_bytes(b"stray")
    server_env(home)
    from core.config import BotConfig
    with pytest.raises(RuntimeError) as exc:
        BotConfig()
    msg = str(exc.value)
    assert str(home / "data" / "database") in msg and str(home / "database") in msg
    assert (home / "data" / "database" / "bot.db").is_file()
    assert (home / "database" / "bot.db-wal").read_bytes() == b"stray"


def test_an_empty_destination_dir_is_not_a_collision(tmp_path):
    home = _v140_home(tmp_path)
    (home / "database").mkdir()
    (home / "characters").mkdir()
    converge_legacy_data_tree(home)
    assert _rows(home / "database" / "bot.db") == [("owner",)]
    assert (home / "characters" / "rob.json").is_file()


def test_a_concurrent_second_caller_is_a_no_op(tmp_path):
    home = _v140_home(tmp_path)
    results, barrier = [], threading.Barrier(4)

    def _run():
        barrier.wait()
        results.append(converge_legacy_data_tree(home))

    threads = [threading.Thread(target=_run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r) == 1
    assert _rows(home / "database" / "bot.db") == [("owner",)]
    assert converge_legacy_data_tree(home) == []


@pytest.mark.parametrize("kw", [{"data_dir": "/srv/x"}, {"db_path": "/srv/x/bot.db"}])
def test_nothing_moves_when_data_dir_or_db_path_is_set(tmp_path, kw):
    home = _v140_home(tmp_path)
    assert converge_legacy_data_tree(home, **kw) == []
    assert (home / "data" / "database" / "bot.db").is_file()
    assert "nothing moves automatically" in legacy_tree_doctor_line(home, **kw)


def test_nothing_moves_when_the_new_db_exists(tmp_path):
    home = _v140_home(tmp_path)
    (home / "database").mkdir()
    sqlite3.connect(home / "database" / "bot.db").close()
    assert converge_legacy_data_tree(home) == []
    assert (home / "data" / "database" / "bot.db").is_file()
    assert "both exist" in legacy_tree_doctor_line(home)


def test_the_update_snapshot_captures_an_unmoved_legacy_tree(tmp_path, server_env):
    home = _v140_home(tmp_path)
    server_env(home)
    from cli.update.context import resolve_update_context
    ctx = resolve_update_context(local=False)
    dbs = {p.resolve() for p in ctx.db_paths}
    assert (home / "data" / "database" / "bot.db").resolve() in dbs
    assert (home / "data" / "artifacts.db").resolve() in dbs
    assert (home / "data" / "characters").resolve() in {p.resolve() for p in ctx.dir_paths}
