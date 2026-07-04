import sqlite3

from scripts.healthcheck import check_goals_db


def test_check_goals_db_ok(tmp_path):
    db = tmp_path / "goals.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE goals (id TEXT)")
    con.commit()
    con.close()
    ok, msg = check_goals_db(str(tmp_path))
    assert ok is True
    assert "goals.db" in msg


def test_check_goals_db_missing(tmp_path):
    ok, msg = check_goals_db(str(tmp_path))
    assert ok is False
