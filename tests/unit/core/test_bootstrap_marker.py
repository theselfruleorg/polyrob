"""The install record (062).

Three behaviours the module exists for: the write is atomic, an unreadable
record is NOT reported as an absent one, and the installer's handoff can never
inject arbitrary keys into the schema.
"""
import json

from core import bootstrap_marker as bm


def test_write_then_read_round_trips(tmp_path):
    path = bm.write_marker(tmp_path, version="1.2.3", install_method="managed",
                           extras=["server", "docs"], installer={})
    assert path is not None and path.name == bm.MARKER_NAME
    status, data = bm.read_marker(tmp_path)
    assert status == bm.OK
    assert data["version"] == "1.2.3"
    assert data["install_method"] == "managed"
    assert data["extras"] == ["docs", "server"]
    assert data["schema_version"] == bm.SCHEMA_VERSION


def test_absent_and_unreadable_are_different_answers(tmp_path):
    assert bm.read_marker(tmp_path)[0] == bm.ABSENT
    bm.marker_path(tmp_path).write_text("{ this is not json")
    assert bm.read_marker(tmp_path)[0] == bm.UNREADABLE
    # ⚠️ The distinction is the point: "absent" means never bootstrapped and
    # "unreadable" means we cannot tell. Collapsing them prints a confident
    # "not installed" over a damaged file.
    assert "unreadable" in bm.describe(tmp_path)


def test_a_marker_without_a_schema_version_is_unreadable(tmp_path):
    bm.marker_path(tmp_path).write_text(json.dumps({"version": "9"}))
    assert bm.read_marker(tmp_path)[0] == bm.UNREADABLE


def test_no_temp_file_survives_a_write(tmp_path):
    bm.write_marker(tmp_path, version="1", installer={})
    assert not list(tmp_path.glob("*.tmp"))


def test_installer_handoff_is_filtered_to_known_keys(tmp_path):
    bm.write_marker(tmp_path, version="1", installer={
        "install_method": "managed", "command": "/usr/local/bin/polyrob",
        "stages": [{"name": "venv", "status": "ok"}, "not a dict"],
        "evil": "value", "source_dir": 42,
    })
    _status, data = bm.read_marker(tmp_path)
    assert "evil" not in data
    assert "source_dir" not in data, "a non-string value is dropped, not coerced"
    assert data["command"] == "/usr/local/bin/polyrob"
    assert data["stages"] == [{"name": "venv", "status": "ok"}]


def test_stages_from_env_tolerates_garbage(monkeypatch):
    monkeypatch.setenv(bm.STAGES_ENV, "not json at all")
    assert bm.stages_from_env() == {}
    monkeypatch.setenv(bm.STAGES_ENV, '["a list"]')
    assert bm.stages_from_env() == {}
    monkeypatch.setenv(bm.STAGES_ENV, '{"install_method":"managed"}')
    assert bm.stages_from_env()["install_method"] == "managed"


def test_describe_names_skipped_stages(tmp_path):
    bm.write_marker(tmp_path, version="1", install_method="managed", installer={
        "stages": [{"name": "browser", "status": "skipped", "detail": "not requested"}],
    })
    line = bm.describe(tmp_path)
    assert "skipped: browser" in line


def test_write_never_raises_on_an_unwritable_home(tmp_path):
    target = tmp_path / "afile"
    target.write_text("not a directory")
    assert bm.write_marker(target / "under-a-file", version="1") is None


def test_the_record_defaults_to_the_config_home_not_the_data_home(tmp_path, monkeypatch):
    """⚠️ The local data home is ``cwd/.polyrob`` BY DESIGN (a per-project
    memory). An install fact written there reads as "never bootstrapped" from
    every other directory — a live install on 2026-09-22 did exactly that."""
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "project-data"))
    written = bm.write_marker(version="9.9.9")
    assert written is not None
    assert written.parent == (tmp_path / "config")
    assert not (tmp_path / "project-data" / bm.MARKER_NAME).exists()
    status, data = bm.read_marker()
    assert status == bm.OK and data["version"] == "9.9.9"
