"""W8 (2026-10-10): the idempotent column-add helper and opt-in user_version."""
import sqlite3

import pytest

from core.sqlite_util import ensure_columns, init_schema, set_user_version


def _cols(path, table):
    with sqlite3.connect(path) as c:
        return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def test_ensure_columns_adds_only_missing(tmp_path):
    db = str(tmp_path / "s.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
        assert ensure_columns(c, "t", [("a", "TEXT"), ("b", "REAL")]) == ["b"]
        assert ensure_columns(c, "t", [("a", "TEXT"), ("b", "REAL")]) == []
    assert _cols(db, "t") == ["id", "a", "b"]


def test_ensure_columns_refuses_non_identifiers(tmp_path):
    with sqlite3.connect(str(tmp_path / "s.db")) as c:
        c.execute("CREATE TABLE t (id INTEGER)")
        with pytest.raises(ValueError):
            ensure_columns(c, "t", [("x; DROP TABLE t", "TEXT")])


def test_init_schema_columns_and_version(tmp_path):
    db = str(tmp_path / "s.db")
    init_schema(db, "CREATE TABLE IF NOT EXISTS t (id INTEGER);")
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 0   # opt-in only
    init_schema(db, "CREATE TABLE IF NOT EXISTS t (id INTEGER);",
                columns={"t": (("note", "TEXT"),)}, version=2)
    assert _cols(db, "t") == ["id", "note"]
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2
        assert set_user_version(c, 1) == 2          # never lowers
