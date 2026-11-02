"""``polyrob doctor`` names a data-home split as CRITICAL (2026-10-29)."""
import sqlite3
from pathlib import Path

import pytest

import core.runtime_paths as rp

_REAL_SERVER = rp._server_default_data_home
_REAL_LOCAL = rp._local_default_data_home


@pytest.fixture
def user_box(tmp_path, monkeypatch):
    home = tmp_path / ".polyrob"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("POLYROB_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.setattr(rp, "_SERVER_DATA_HOME", tmp_path / "none" / "polyrob")
    monkeypatch.setattr(rp, "_server_default_data_home", _REAL_SERVER)
    monkeypatch.setattr(rp, "_local_default_data_home", _REAL_LOCAL)
    monkeypatch.chdir(tmp_path)
    return home


def _db(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()


def test_two_bot_dbs_are_critical(user_box):
    from cli.commands.doctor_data_home import data_home_split_lines
    _db(user_box / "data" / "database" / "bot.db")
    _db(user_box / "database" / "bot.db")  # what the old migrate step created
    lines = data_home_split_lines({})
    crit = [l for l in lines if l.startswith("CRITICAL")]
    assert crit and str(user_box / "database" / "bot.db") in crit[0]
    assert "POLYROB_DATA_DIR" in crit[0]


def test_one_home_is_a_plain_line(user_box):
    from cli.commands.doctor_data_home import data_home_split_lines
    _db(user_box / "data" / "database" / "bot.db")
    lines = data_home_split_lines({})
    assert not any(l.startswith("CRITICAL") for l in lines), lines
    assert any(l.startswith("data home:") for l in lines)


def test_an_old_unit_without_the_pin_is_named(user_box):
    from cli.commands.doctor_data_home import data_home_split_lines
    unit = user_box.parent / "cfg" / "systemd" / "user" / "polyrob.service"
    unit.parent.mkdir(parents=True)
    unit.write_text('[Service]\nEnvironment="POLYROB_HOME=/x"\n')
    assert any("polyrob service install" in l for l in data_home_split_lines({}))


def test_doctor_report_carries_the_host_lines(monkeypatch):
    from cli.commands import doctor_host
    monkeypatch.setattr(doctor_host, "_checkers", lambda: [lambda env: ["CRITICAL x"]])
    assert doctor_host.host_readiness_lines({}) == ["CRITICAL x"]


def test_plain_doctor_headline_carries_a_critical_host_item(monkeypatch):
    from cli.commands import doctor, doctor_host
    monkeypatch.setattr(doctor_host, "_checkers",
                        lambda: [lambda env: ["CRITICAL data home split: x", "data home: y"]])
    from types import SimpleNamespace
    snap = SimpleNamespace(sections={})
    monkeypatch.setattr(doctor, "build_doctor_snapshot", lambda: (snap, None))
    monkeypatch.setattr("core.status_render.render_status_lines", lambda *a, **k: ["Health: OK"])
    monkeypatch.setattr(doctor, "credential_alert_lines", lambda env: [])
    lines = doctor.status_snapshot_lines()
    assert "CRITICAL data home split: x" in lines
    assert "data home: y" not in lines
