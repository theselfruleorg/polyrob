"""R-2 (B1/B2): DB_PATH is real — config default matches reality and the
database manager honors an explicit DB_PATH behind a refuse-to-guess guard.

Before this wave, ``config.db_path`` (env ``DB_PATH``) was a decoy: config
anchored it, created its parent dir, and the update snapshot trusted it — but
``database_manager`` hardcoded ``<data_dir>/database/bot.db`` and never read it.
"""
import os
from pathlib import Path

import pytest


@pytest.fixture()
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DB_PATH", raising=False)
    # Anchor ALL config paths to tmp (config otherwise anchors to base_dir =
    # the code root, and BotConfig creates directories at construction).
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield


def _bot_config(**kw):
    from core.config import BotConfig
    return BotConfig(**kw)


def test_default_db_path_matches_manager_reality(_clean_env):
    """B1: the documented default must equal the file the app actually opens
    (<data_dir>/database/bot.db) — not the historical decoy data/bot.db."""
    cfg = _bot_config()
    assert Path(cfg.db_path).resolve() == (
        Path(cfg.data_dir) / "database" / "bot.db").resolve()


def test_manager_uses_legacy_derivation_when_db_path_env_unset(_clean_env, tmp_path):
    """B2: without DB_PATH, resolution is byte-identical to the historical
    <data_dir>/database/bot.db (the CLI reassigns data_dir post-construction —
    the derivation must follow it)."""
    from modules.database.database_manager import resolve_bot_db_path
    cfg = _bot_config()
    cfg.data_dir = str(tmp_path / "reassigned")  # CLI-style post-construction move
    assert resolve_bot_db_path(cfg) == Path(cfg.data_dir) / "database" / "bot.db"


def test_manager_honors_explicit_db_path_env(_clean_env, tmp_path, monkeypatch):
    """B2: DB_PATH set and no legacy file → the configured path is opened."""
    custom = tmp_path / "custom" / "bot.db"
    monkeypatch.setenv("DB_PATH", str(custom))
    from modules.database.database_manager import resolve_bot_db_path
    cfg = _bot_config()
    assert resolve_bot_db_path(cfg) == custom


def test_manager_refuses_to_guess_between_diverging_paths(_clean_env, tmp_path, monkeypatch):
    """B2 guard: DB_PATH points somewhere new while the REAL database still sits
    at the legacy location → refuse loudly (never silently open a fresh empty DB,
    never silently move a live one)."""
    custom = tmp_path / "custom" / "bot.db"
    monkeypatch.setenv("DB_PATH", str(custom))
    from modules.database.database_manager import resolve_bot_db_path
    cfg = _bot_config()
    legacy = Path(cfg.data_dir) / "database" / "bot.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"SQLite format 3\x00")
    with pytest.raises(RuntimeError, match="DB_PATH"):
        resolve_bot_db_path(cfg)


def test_manager_accepts_db_path_env_equal_to_legacy(_clean_env, tmp_path, monkeypatch):
    """B2: prod shape — DB_PATH explicitly set to the same file the derivation
    yields must resolve cleanly even when the DB exists."""
    from modules.database.database_manager import resolve_bot_db_path
    cfg = _bot_config()
    legacy = Path(cfg.data_dir) / "database" / "bot.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"SQLite format 3\x00")
    monkeypatch.setenv("DB_PATH", str(legacy))
    assert resolve_bot_db_path(cfg) == legacy
