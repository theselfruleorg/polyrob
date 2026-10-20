"""080 WS-1 item 4 — "Send /restart" only when the written file is on the
running service's env ladder (its boot record names the ladder)."""
import pytest

from core import lifecycle as lc


@pytest.fixture
def svc(tmp_path, monkeypatch):
    home = tmp_path / "data"
    home.mkdir()
    monkeypatch.setattr(lc, "supervisor", lambda: "systemd")
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "svc-home"))
    rec = lc.record_boot(data_home=home)
    return home, rec


def test_boot_record_names_the_ladder(svc, tmp_path):
    _home, rec = svc
    assert str(tmp_path / "svc-home" / ".env") in rec["env_files"]


def test_hint_when_the_file_is_on_the_ladder(svc, tmp_path):
    home, _ = svc
    hint = lc.restart_hint("telegram", data_home=home, written=tmp_path / "svc-home" / ".env")
    assert hint == "Send /restart to apply it now."


def test_no_restart_promise_for_a_file_the_service_never_reads(svc, tmp_path):
    home, _ = svc
    other = tmp_path / "console-home" / ".env"
    hint = lc.restart_hint("console", data_home=home, written=other)
    assert "Send /restart" not in hint
    assert str(other) in hint and "does not read" in hint


def test_an_old_boot_record_without_a_ladder_promises_nothing(svc):
    home, rec = svc
    rec.pop("env_files")
    lc._write_json(lc._boot_file(lc.lifecycle_dir(home)), rec)
    assert lc.restart_hint("telegram", data_home=home, written="/x/.env") == ""
