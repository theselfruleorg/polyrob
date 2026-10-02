"""`polyrob kb reindex` — the WS-K3 backfill.

⚠️ This file exists because the first revalidation pass found a `NameError` in
the summary line: the verb ran, picked its files, and then crashed on the last
statement. A verb whose happy path is never executed in a test is a verb that
has not been run.
"""
import os
import sqlite3

import pytest
from click.testing import CliRunner

from cli.commands.kb import kb


@pytest.fixture
def home(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "brief.md").write_text("# brief\n\nreal knowledge\n")
    (docs / "exit-run-log.md").write_text("# log\n\nrow row\n")
    (docs / "shot.png").write_text("x")
    (docs / "backup.md.bak").write_text("# an old copy\n")

    db = tmp_path / "artifacts.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE artifacts (id TEXT, user_id TEXT, session_id TEXT, "
                "path TEXT)")
    for i, name in enumerate(["brief.md", "exit-run-log.md", "shot.png",
                              "backup.md.bak", "gone.md"]):
        con.execute("INSERT INTO artifacts VALUES (?,?,?,?)",
                    (str(i), "rob", "sess-123", str(docs / name)))
    con.commit()
    con.close()

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KB_ENABLED", "1")
    return tmp_path


def test_dry_run_names_what_it_would_index_and_changes_nothing(home):
    result = CliRunner().invoke(kb, ["reindex", "--user", "rob", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "5 registered · 1 to index" in result.output
    assert "1 gone from disk" in result.output
    assert "would index" in result.output and "brief.md" in result.output
    # a run log, a screenshot and a .bak copy are not knowledge
    assert "exit-run-log.md" not in result.output
    assert "shot.png" not in result.output
    assert "backup.md.bak" not in result.output


def test_it_says_so_when_there_is_no_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KB_ENABLED", "1")

    result = CliRunner().invoke(kb, ["reindex", "--user", "rob", "--dry-run"])

    assert result.exit_code == 2
    assert "no artifact registry" in result.output


def test_it_refuses_when_the_kb_is_off(home, monkeypatch):
    monkeypatch.setenv("KB_ENABLED", "false")
    monkeypatch.setenv("POLYROB_LOCAL", "0")

    result = CliRunner().invoke(kb, ["reindex", "--user", "rob", "--dry-run"])

    assert result.exit_code == 2
    assert "KB disabled" in result.output


def test_the_ingest_carries_each_row_s_own_session(home, monkeypatch):
    """⚠️ An empty session id resolves to a workspace that does not exist — and
    off project-root mode the resolver would CREATE one. A backfill must not
    invent a session directory."""
    seen = []

    async def fake_ingest(user_id, path, *, session_id="", force=False):
        seen.append((path, session_id, force))
        return {"ingested": 1}

    import tools.kb_autoingest as ai
    monkeypatch.setattr(ai, "ingest_artifact", fake_ingest)

    result = CliRunner().invoke(kb, ["reindex", "--user", "rob"])

    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    assert seen[0][1] == "sess-123" and seen[0][2] is True
    assert "indexed 1" in result.output
