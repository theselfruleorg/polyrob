"""3.3 (U10/O6, 2026-07-14 review) — doctor environment checks.

Doctor gains: Python-version floor, [server]-extra presence, a Playwright
chromium probe, and a DB-schema-vs-code check (the code's migration HEAD vs
what bot.db actually recorded). `polyrob update` prints the schema line too.
"""
import sqlite3
from pathlib import Path

from cli.commands.doctor import (
    doctor_report,
    playwright_line,
    python_version_line,
    schema_status_line,
    server_extra_line,
)


# ---------------------------------------------------------------------------
# python version
# ---------------------------------------------------------------------------

def test_python_version_line_present_in_report():
    assert any(l.startswith("python:") for l in doctor_report({}))


def test_python_version_warns_below_floor(monkeypatch):
    import cli.commands.doctor as d

    class _V:
        major, minor, micro = 3, 10, 4

    monkeypatch.setattr(d.sys, "version_info", _V)
    assert "requires Python >= 3.11" in python_version_line()


def test_python_version_no_warning_at_floor():
    # The suite itself runs on >= 3.11, so the live line must not warn.
    assert "requires" not in python_version_line()


# ---------------------------------------------------------------------------
# [server] extra
# ---------------------------------------------------------------------------

def test_server_extra_absent_branch(monkeypatch):
    import cli.commands.doctor as d
    monkeypatch.setattr(d.importlib.util, "find_spec", lambda name: None)
    line = server_extra_line()
    assert "absent" in line and "polyrob[server]" in line


def test_server_extra_present_branch(monkeypatch):
    import cli.commands.doctor as d
    monkeypatch.setattr(d.importlib.util, "find_spec", lambda name: object())
    assert "present" in server_extra_line()


# ---------------------------------------------------------------------------
# playwright probe
# ---------------------------------------------------------------------------

def test_playwright_not_installed(monkeypatch):
    import cli.commands.doctor as d
    monkeypatch.setattr(d.importlib.util, "find_spec", lambda name: None)
    line = playwright_line({})
    assert "not installed" in line


def test_playwright_installed_with_chromium(monkeypatch, tmp_path):
    import cli.commands.doctor as d
    monkeypatch.setattr(d.importlib.util, "find_spec", lambda name: object())
    (tmp_path / "chromium-1234").mkdir()
    line = playwright_line({"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path)})
    assert "chromium present" in line


def test_playwright_installed_without_chromium(monkeypatch, tmp_path):
    import cli.commands.doctor as d
    monkeypatch.setattr(d.importlib.util, "find_spec", lambda name: object())
    line = playwright_line({"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path)})
    assert "playwright install chromium" in line


# ---------------------------------------------------------------------------
# db schema vs code
# ---------------------------------------------------------------------------

def _mk_bot_db(data_home: Path, versions):
    db_path = data_home / "database" / "bot.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE schema_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            applied_by TEXT DEFAULT 'system',
            checksum TEXT, execution_time_ms INTEGER)
    """)
    for v in versions:
        con.execute("INSERT INTO schema_versions (version, description) VALUES (?, ?)",
                    (v, f"test {v}"))
    con.commit()
    con.close()
    return db_path


def test_schema_status_no_db(tmp_path):
    line = schema_status_line({"POLYROB_DATA_DIR": str(tmp_path)})
    assert "no bot.db" in line


def test_schema_status_up_to_date(tmp_path):
    from migrations.version_manager import latest_migration_version
    head = latest_migration_version()
    _mk_bot_db(tmp_path, ["1.0.0", head])
    line = schema_status_line({"POLYROB_DATA_DIR": str(tmp_path)})
    assert "up to date" in line and head in line


def test_schema_status_behind_code(tmp_path):
    _mk_bot_db(tmp_path, ["1.0.0"])
    line = schema_status_line({"POLYROB_DATA_DIR": str(tmp_path)})
    assert "behind" in line and "migrations.migrate upgrade" in line


def test_schema_status_unversioned_db(tmp_path):
    db_path = tmp_path / "database" / "bot.db"
    db_path.parent.mkdir(parents=True)
    sqlite3.connect(db_path).close()  # exists, no schema_versions table
    line = schema_status_line({"POLYROB_DATA_DIR": str(tmp_path)})
    assert "not versioned" in line


def test_doctor_report_includes_new_checks(tmp_path):
    blob = "\n".join(doctor_report({"POLYROB_DATA_DIR": str(tmp_path)}))
    for marker in ("python:", "server extra:", "playwright:", "db schema:"):
        assert marker in blob, f"doctor report missing {marker!r}"


def test_update_command_prints_schema_check():
    """`polyrob update` surfaces the same schema-vs-code line (U10)."""
    import inspect
    import cli.commands.update as u
    assert "schema_status_line" in inspect.getsource(u)
