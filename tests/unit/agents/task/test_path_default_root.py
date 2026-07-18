"""Bare PathManager() must route through resolve_session_data_root (audit T10).

RC-1 landmine (2026-07-07): a process that skipped build_cli_container fell back
to the raw ``DATA_ROOT`` env → ``./data/task`` even when ``POLYROB_DATA_DIR`` was
set — minting a second, divergent session tree (the webview "two trees" bug
class). The shared resolver already encodes the right precedence
(``DATA_ROOT`` → ``POLYROB_DATA_DIR/sessions`` → ``./data/task``); the default
constructor must use it.
"""
from pathlib import Path


def test_bare_pathmanager_honors_polyrob_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from agents.task.path import PathManager
    assert Path(PathManager().data_root).resolve() == (tmp_path / "sessions").resolve()


def test_bare_pathmanager_data_root_env_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "explicit"))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.path import PathManager
    assert Path(PathManager().data_root).resolve() == (tmp_path / "explicit").resolve()


def test_bare_pathmanager_legacy_default_unchanged(monkeypatch):
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    from agents.task.path import PathManager
    assert Path(PathManager().data_root).resolve() == Path("./data/task").resolve()
