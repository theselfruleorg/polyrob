"""Low (2026-09-23 analysis): env files and the dev Fernet key are created with no
permissive window, replaced atomically, kept under the data home, and the key is
not copied into os.environ."""
import os
import stat

import pytest

from core import env_file
from core.env_file import read_env_file, remove_env_var, upsert_env_var, write_private_file


def test_upsert_creates_0600_atomically_and_never_chmods_after(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    calls = []
    real_chmod = os.chmod
    monkeypatch.setattr(os, "chmod", lambda *a, **k: (calls.append(a), real_chmod(*a, **k)))
    upsert_env_var(p, "OPENAI_API_KEY", "sk-test")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert calls == []  # the mode is set on the descriptor before the rename
    assert read_env_file(p) == {"OPENAI_API_KEY": "sk-test"}
    assert [n for n in os.listdir(tmp_path) if n.endswith(".tmp")] == []


def test_insecure_write_keeps_the_existing_mode(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n")
    os.chmod(p, 0o640)
    upsert_env_var(p, "B", "2", secure=False)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640
    assert remove_env_var(p, "A", secure=False)
    assert read_env_file(p) == {"B": "2"}


def test_failed_write_leaves_the_old_file_and_no_temp(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    upsert_env_var(p, "A", "1")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(env_file.os, "replace", boom)
    with pytest.raises(OSError):
        upsert_env_var(p, "A", "2")
    assert read_env_file(p) == {"A": "1"}
    assert [n for n in os.listdir(tmp_path) if n.endswith(".tmp")] == []


def test_own_symlink_is_written_through(tmp_path):
    real = tmp_path / "dotfiles.env"
    real.write_text("A=1\n")
    link = tmp_path / ".env"
    os.symlink(str(real), str(link))
    upsert_env_var(link, "B", "2")
    assert link.is_symlink() and read_env_file(real) == {"A": "1", "B": "2"}


def test_write_private_file_bytes(tmp_path):
    p = tmp_path / "sub" / "k"
    write_private_file(p, b"secret")
    assert p.read_bytes() == b"secret" and stat.S_IMODE(os.stat(p).st_mode) == 0o600


@pytest.fixture
def enc(tmp_path, monkeypatch):
    import core.security.encryption as e
    monkeypatch.delenv("MCP_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("POLYROB_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(e, "_legacy_key_file_path", lambda: tmp_path / "legacy" / ".mcp_encryption_key")
    e._DEV_KEYS.clear()
    yield e
    e._DEV_KEYS.clear()


def test_dev_key_lives_in_the_data_home_0600_and_stays_out_of_environ(enc, tmp_path):
    key = enc._load_or_generate_key()
    p = tmp_path / "home" / ".mcp_encryption_key"
    assert p.read_text() == key
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert "MCP_ENCRYPTION_KEY" not in os.environ
    enc._DEV_KEYS.clear()
    assert enc._load_or_generate_key() == key  # reused across a restart


def test_legacy_install_tree_key_is_migrated_not_replaced(enc, tmp_path):
    from cryptography.fernet import Fernet
    legacy = tmp_path / "legacy" / ".mcp_encryption_key"
    legacy.parent.mkdir()
    old = Fernet.generate_key().decode()
    legacy.write_text(old)
    assert enc._load_or_generate_key() == old
    assert (tmp_path / "home" / ".mcp_encryption_key").read_text() == old


def test_symlinked_key_file_is_refused(enc, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "evil").write_text("x")
    os.symlink(str(tmp_path / "evil"), str(home / ".mcp_encryption_key"))
    with pytest.raises(RuntimeError):
        enc._load_or_generate_key()
