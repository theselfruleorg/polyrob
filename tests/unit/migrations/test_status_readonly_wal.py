"""`migrate status` on a WAL bot.db in a directory the reader cannot write.

A read-only open of a WAL database must create the -shm file. With no -wal or
-shm present and a directory the reader cannot write, SQLite answers "attempt
to write a readonly database". The status names that cause in one plain line.
"""
import os
import sqlite3

import pytest

from migrations import migrate


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root can write any directory")
def test_status_names_the_wal_cause_in_a_directory_it_cannot_write(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / "database"
    home.mkdir()
    path = home / "bot.db"
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE schema_versions (id INTEGER PRIMARY KEY, version TEXT)")
    con.commit()
    con.close()                                  # the last close removes -wal/-shm
    monkeypatch.setenv("DB_PATH", str(path))
    os.chmod(home, 0o555)
    try:
        assert migrate.display_status_readonly() is False
    finally:
        os.chmod(home, 0o755)
    out = capsys.readouterr().out
    assert "WAL database" in out
    assert str(home) in out
