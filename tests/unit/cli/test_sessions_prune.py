"""`polyrob sessions prune` — the WS-K2 owner seat.

The only verb in the knowledge work that DELETES anything, so its happy path,
its refusal and its confirmation all run here. (The sibling backfill verb
shipped with a NameError in exactly the line a test like this executes.)
"""
import os
import sqlite3

import pytest
from click.testing import CliRunner

from cli.commands.session import session

NOW_DAYS = 400
ANCIENT = "6f1c3b0e-0000-4000-8000-000000000001"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "sessions" / "rob"
    root.mkdir(parents=True)
    # Only UUID-named trees are session trees (H13).
    old, new = root / ANCIENT, root / "6f1c3b0e-0000-4000-8000-000000000002"
    for d in (old, new):
        (d / "workspace").mkdir(parents=True)
    stamp = os.stat(tmp_path).st_mtime - NOW_DAYS * 86400
    # Age is the newest mtime anywhere in the tree, so age the whole tree.
    os.utime(old / "workspace", (stamp, stamp))
    os.utime(old, (stamp, stamp))

    con = sqlite3.connect(tmp_path / "goals.db")
    con.execute("CREATE TABLE goals (id TEXT, user_id TEXT, status TEXT, "
                "session_id TEXT, payload TEXT)")
    con.execute("INSERT INTO goals VALUES ('g1','rob','done',?,'{}')", (ANCIENT,))
    con.commit()
    con.close()

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "90")
    return tmp_path, old, new


def test_dry_run_names_the_plan_and_deletes_nothing(home):
    tmp_path, old, new = home

    result = CliRunner().invoke(session, ["prune", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "2 trees scanned · 1 to remove" in result.output
    assert "1 recent" in result.output
    assert str(old) in result.output
    assert old.exists() and new.exists()


def test_it_asks_before_deleting_and_a_no_keeps_everything(home):
    tmp_path, old, new = home

    result = CliRunner().invoke(session, ["prune"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "cancelled" in result.output
    assert old.exists()


def test_yes_removes_exactly_what_the_plan_named(home):
    tmp_path, old, new = home

    result = CliRunner().invoke(session, ["prune", "--yes"])

    assert result.exit_code == 0, result.output
    assert "removed 1 · failed 0" in result.output
    assert not old.exists()
    assert new.exists()


def test_a_live_goal_keeps_its_tree(home):
    tmp_path, old, new = home
    con = sqlite3.connect(tmp_path / "goals.db")
    con.execute("UPDATE goals SET status='running' WHERE id='g1'")
    con.commit()
    con.close()

    result = CliRunner().invoke(session, ["prune", "--yes"])

    assert result.exit_code == 0, result.output
    assert "0 to remove" in result.output
    assert "1 named by a goal" in result.output
    assert old.exists()


def test_an_unreadable_goal_board_refuses_the_sweep(home):
    tmp_path, old, new = home
    with open(tmp_path / "goals.db", "wb") as fh:
        fh.write(b"not a database")

    result = CliRunner().invoke(session, ["prune", "--yes"])

    assert result.exit_code == 1
    assert "refused" in result.output and "goals.db unreadable" in result.output
    assert old.exists()


def test_retention_disabled_removes_nothing(home, monkeypatch):
    tmp_path, old, new = home
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "0")

    result = CliRunner().invoke(session, ["prune", "--yes"])

    # exit 2 = "did nothing on purpose", the same code `polyrob kb` uses when its
    # feature is off, so a script can tell it from a successful sweep of zero.
    assert result.exit_code == 2
    assert "session retention is OFF" in result.output
    assert "SESSION_RETENTION_DAYS" in result.output   # the knob, since there is no pref
    assert old.exists()
