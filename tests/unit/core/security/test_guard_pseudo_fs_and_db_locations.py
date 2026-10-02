"""Security analysis 2026-09-23 (low): /proc, /sys and data-home databases."""
from pathlib import Path

import pytest

from core.security.secret_guard import is_credential_file, is_secret_path


@pytest.mark.parametrize("p", ["/proc/self/environ", "/proc/1/environ",
                               "/proc/42/cmdline", "/sys/kernel/debug/x", "/proc"])
def test_pseudo_fs_refused(p):
    assert is_credential_file(Path(p))
    assert is_secret_path(Path(p), root=Path("/"))


@pytest.mark.parametrize("p", ["/processes/a.txt", "/system/notes.md", "/srv/proc/x"])
def test_lookalikes_allowed(p):
    assert not is_credential_file(Path(p))


@pytest.mark.parametrize("name", ["app_services.db", "memory.db", "dapp_sessions.db",
                                  "session_registry.db", "token_denylist.db"])
def test_named_stores_refused_anywhere(tmp_path, name):
    assert is_secret_path(tmp_path / "proj" / name, root=tmp_path)


def test_credential_file_keeps_owner_dbs_exportable(tmp_path):
    # is_credential_file also decides what `polyrob profile export` omits: the
    # owner's memory / revoked-token list must stay in a backup. The file
    # tools are kept off them by the data-home deny seam instead (H12).
    assert is_credential_file(tmp_path / "dapp_sessions.db")
    for name in ("memory.db", "token_denylist.db", "goals.db"):
        assert not is_credential_file(Path(name))


def test_any_db_directly_in_data_home_refused(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    for name in ("goals.db", "cron.db", "pairing.db", "x.sqlite3", "goals.db-wal"):
        assert is_secret_path(home / name, root=tmp_path), name
    # A project's own database elsewhere is not a data-home store.
    assert not is_secret_path(tmp_path / "proj" / "app.db", root=tmp_path)
    assert not is_secret_path(home / "sub" / "app.db", root=tmp_path)


def test_symlink_to_data_home_db_refused(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "goals.db").write_text("")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    link_dir = tmp_path / "ws"
    link_dir.mkdir()
    (link_dir / "h").symlink_to(home)
    assert is_secret_path(link_dir / "h" / "goals.db", root=tmp_path)
