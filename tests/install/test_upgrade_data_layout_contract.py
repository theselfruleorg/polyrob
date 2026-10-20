"""Upgrade contract: every released data-home layout is found by the current code.

A server install keeps its data under ``POLYROB_DATA_DIR``. Each row below is the
layout a RELEASED version wrote (bot.db plus a store that hung under
``config.data_dir``). The current code must open that bot.db with its rows and
find the store, with no operator step. A future layout change that orphans a
row fails here — add a convergence step, never delete a row.
"""
import sqlite3
from pathlib import Path

import pytest

# version -> (config.data_dir under POLYROB_DATA_DIR, a store under config.data_dir).
# bot.db sat at <config.data_dir>/database/bot.db in every release.
RELEASED_SERVER_LAYOUTS = {
    "1.4.0": ("data", "characters/rob.json"),
}


@pytest.mark.parametrize("version", sorted(RELEASED_SERVER_LAYOUTS))
def test_a_released_server_data_home_is_found(version, tmp_path, monkeypatch):
    data_rel, store_rel = RELEASED_SERVER_LAYOUTS[version]
    home = tmp_path / "home"
    db = home / data_rel / "database" / "bot.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE marker (v TEXT)")
    con.execute("INSERT INTO marker VALUES (?)", (version,))
    con.commit()
    con.close()
    (home / data_rel / store_rel).parent.mkdir(parents=True, exist_ok=True)
    (home / data_rel / store_rel).write_text(version)

    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    for var in ("DATA_DIR", "DB_PATH", "POLYROB_LOCAL"):
        monkeypatch.delenv(var, raising=False)
    from core.config import BotConfig
    from modules.database.database_manager import resolve_bot_db_path
    cfg = BotConfig()
    opened = resolve_bot_db_path(cfg)
    con = sqlite3.connect(opened)
    try:
        assert con.execute("SELECT v FROM marker").fetchall() == [(version,)]
    finally:
        con.close()
    assert (Path(cfg.data_dir) / store_rel).read_text() == version
