"""`cli/update/context.py` — snapshot input resolution.

The context is the SSOT for *what* gets snapshot/restored. The most dangerous gap is
bot.db: its real path is config-driven (``DB_PATH``), so the context must resolve it
and never rely on a single guessed layout — otherwise a rollback silently skips the
live DB (data-loss).
"""
import os

import pytest

from cli.update.context import resolve_update_context


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    return p


def test_db_path_env_is_captured(monkeypatch, tmp_path):
    data_home = tmp_path / "home"
    # A prod-style absolute DB_PATH pointing OUTSIDE data_home.
    real_db = tmp_path / "opt" / "polyrob" / "data" / "database" / "bot.db"
    _touch(real_db)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(data_home))
    monkeypatch.setenv("DB_PATH", str(real_db))

    uctx = resolve_update_context()
    assert real_db.resolve() in uctx.db_paths


def test_sidecars_captured_alongside_bot_db(monkeypatch, tmp_path):
    data_home = tmp_path / "home"
    _touch(data_home / "memory.db")
    _touch(data_home / "goals.db")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(data_home))
    monkeypatch.delenv("DB_PATH", raising=False)

    uctx = resolve_update_context()
    names = {p.name for p in uctx.db_paths}
    assert {"memory.db", "goals.db"} <= names


def test_server_mode_config_and_db_captured(monkeypatch, tmp_path):
    # Server posture: secrets + DB_PATH live in code_root/config/.env.production, and
    # the real bot.db is outside data_home. A rollback must restore both.
    import core.runtime_paths as rpmod

    code_root = tmp_path / "opt" / "polyrob"
    data_home = tmp_path / "var" / "lib" / "polyrob"
    (code_root / "config").mkdir(parents=True)
    real_db = code_root / "data" / "database" / "bot.db"
    _touch(real_db)
    (code_root / "config" / ".env.production").write_text(f"DB_PATH={real_db}\n")

    def fake_paths(*, local):
        return rpmod.RuntimePaths(
            code_root=code_root,
            config_dir=data_home,   # local resolution => config_dir == data_home
            data_home=data_home,
            workspace_root=data_home / "task",
        )

    monkeypatch.setattr(rpmod, "resolve_runtime_paths", fake_paths)
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)

    uctx = resolve_update_context()
    assert (code_root / "config" / ".env.production") in uctx.config_paths
    assert real_db.resolve() in uctx.db_paths


def test_db_path_read_from_env_file_when_unset(monkeypatch, tmp_path):
    # No DB_PATH in the environment, but the config .env pins it.
    data_home = tmp_path / "home"
    real_db = tmp_path / "srv" / "bot.db"
    _touch(real_db)
    _touch(data_home / ".env")
    (data_home / ".env").write_text(f"DB_PATH={real_db}\n")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(data_home))
    monkeypatch.delenv("DB_PATH", raising=False)

    uctx = resolve_update_context()
    assert real_db.resolve() in uctx.db_paths
