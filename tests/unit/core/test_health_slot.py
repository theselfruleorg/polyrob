"""120 W8 — the health slot (``core.health_slot``): ONE home for the install
checks that name no credential. `polyrob doctor --full` prints each line at its
old place; `agent_status(view="health")` prints :func:`health_slot_lines`.

§4.3 of the proposal: a check that could not run is named, never shown as
healthy; the agent's read starts no probe `/status` does not make, loads no pack,
prints no path of the host and no secret.

Every test runs in a temp home: no network, no real env file, no credential.
"""
import sqlite3
from pathlib import Path

import pytest

import core.health_slot as hs
import core.runtime_paths as rp


@pytest.fixture(autouse=True)
def _temp_home(tmp_path, monkeypatch):
    home = tmp_path / "user"
    (home / ".polyrob").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("POLYROB_HOME", str(home / ".polyrob"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rp, "_SERVER_DATA_HOME", tmp_path / "none" / "polyrob")
    (tmp_path / "data").mkdir()
    yield


def _versioned_db(data_home: Path, versions):
    db = data_home / "database" / "bot.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE schema_versions (version TEXT)")
    con.executemany("INSERT INTO schema_versions VALUES (?)", [(v,) for v in versions])
    con.commit()
    con.close()


# --- the schema line and its hook -------------------------------------------------------

def test_schema_without_the_migrations_tier_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "_SCHEMA_HEAD", None)
    _versioned_db(tmp_path / "data", ["1.0.0"])
    assert hs.schema_status_line(tmp_path / "data") == (
        "db schema: not checked here (this process did not load the migrations; "
        "`polyrob doctor --full` checks it)")


def test_schema_head_that_raises_names_the_error(tmp_path):
    def boom():
        raise RuntimeError("no versions dir")
    assert hs.schema_status_line(tmp_path / "data", code_head=boom) == (
        "db schema: unknown (could not resolve code schema version: RuntimeError)")


def test_schema_reads_the_registered_head(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "_SCHEMA_HEAD", None)
    hs.register_schema_head(lambda: "2.0.0")
    _versioned_db(tmp_path / "data", ["1.0.0", "2.0.0"])
    assert hs.schema_status_line(tmp_path / "data") == "db schema: 2.0.0 (up to date)"


def test_the_migrations_tier_registers_its_head():
    import migrations.version_manager as vm
    assert hs._SCHEMA_HEAD is vm.latest_migration_version


def test_an_unreadable_db_is_named_never_up_to_date(tmp_path):
    db = tmp_path / "data" / "database" / "bot.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a sqlite file at all" * 64)
    line = hs.schema_status_line(tmp_path / "data", code_head=lambda: "1.0.0")
    assert line.startswith("db schema: unreadable (")
    assert "up to date" not in line


def test_the_doctor_prints_the_slot_lines_byte_for_byte(tmp_path):
    """§5: the seat and the agent print the same text for the same input."""
    from cli.commands.doctor import doctor_report, schema_status_line
    from migrations.version_manager import latest_migration_version
    env = {"POLYROB_DATA_DIR": str(tmp_path / "data")}
    report = doctor_report(env)
    expected = [hs.schema_status_line(tmp_path / "data", code_head=latest_migration_version),
                hs.optional_extras_line(), hs.server_extra_line(), hs.playwright_line(env)]
    for line in expected:
        assert line in report, line
    assert schema_status_line(env) == expected[0]


# --- the data-home layout ---------------------------------------------------------------

def test_the_data_home_all_clear_says_when_the_unit_was_not_read(tmp_path):
    env = {"POLYROB_DATA_DIR": str(tmp_path / "data")}
    seat = hs.data_home_split_lines(env)
    agent = hs.data_home_split_lines(env, unit_read=False)
    assert seat == [f"data home: {tmp_path / 'data'} (one home for every process)"]
    assert agent == [f"data home: {tmp_path / 'data'} (one home for every process; "
                     "the service unit is not read in this process)"]


def test_the_cli_wrapper_adds_only_the_unit(tmp_path, monkeypatch):
    from cli.commands import doctor_data_home as cli_dh
    unit = tmp_path / "polyrob.service"
    monkeypatch.setattr(cli_dh, "_unit_lacks_data_dir", lambda: unit)
    env = {"POLYROB_DATA_DIR": str(tmp_path / "data")}
    assert cli_dh.data_home_split_lines(env) == hs.data_home_split_lines(
        env, unit_without_pin=unit)


# --- the agent's read -------------------------------------------------------------------

def test_mask_paths_names_the_roots_and_keeps_the_layout(tmp_path):
    data = tmp_path / "data"
    home = tmp_path / "user"
    text = (f"bot.db exists at {data}/database/bot.db, {home}/.polyrob/database/bot.db "
            f"and {home}/other; {tmp_path}/datax stays")
    out = hs.mask_paths(text, data)
    assert "<data home>/database/bot.db" in out
    assert "<polyrob home>/database/bot.db" in out
    assert "~/other" in out
    assert f"{tmp_path}/datax" in out          # a prefix of another name is not a root
    assert hs.mask_paths("data home: data", "data") == "data home: data"   # relative: kept


def test_the_agent_read_makes_no_remote_probe_and_loads_no_pack(tmp_path, monkeypatch):
    asked = {}

    def _rail(**kw):
        asked.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr("core.security.browser_rail.browser_rail_status", _rail)

    def _no_load():
        raise AssertionError("the agent's read loads no pack")

    monkeypatch.setattr("core.packs.loader.load_packs", _no_load)
    monkeypatch.setattr("core.security.sandbox_doctor.docker_sandbox_lines",
                        lambda env: ["docker socket: (test)"])
    lines = hs.health_slot_lines({}, tmp_path / "data")
    assert asked == {"refresh": False, "probe": False}
    assert "browser rail: unreadable (RuntimeError)" in lines
    assert any(ln.startswith("packs: ") for ln in lines)


def test_a_check_that_raised_is_named(tmp_path, monkeypatch):
    def _boom(env):
        raise OSError("no /proc here")

    monkeypatch.setattr("core.security.sandbox_doctor.docker_sandbox_lines", _boom)
    lines = hs.health_slot_lines({}, tmp_path / "data")
    assert "docker sandbox: could not be checked (OSError: no /proc here)" in lines


def test_the_agent_read_prints_no_host_path_and_no_secret(tmp_path, monkeypatch):
    secret = "sk-test-" + "a" * 40
    monkeypatch.setattr(hs, "_SCHEMA_HEAD", lambda: "2.0.0")
    _versioned_db(tmp_path / "data", ["1.0.0"])
    _versioned_db(tmp_path / "user" / ".polyrob", ["1.0.0"])   # a second bot.db: a split
    monkeypatch.setattr("core.security.sandbox_doctor.docker_sandbox_lines",
                        lambda env: [f"docker socket: key {secret}"])
    lines = hs.health_slot_lines({"OPENAI_API_KEY": secret}, tmp_path / "data")
    text = "\n".join(lines)
    assert "CRITICAL data home split" in text
    assert str(tmp_path) not in text
    assert secret not in text
    assert any(ln.startswith("! db schema 1.0.0 behind code 2.0.0") for ln in lines)
