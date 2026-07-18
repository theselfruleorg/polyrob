"""Runtime logs must resolve to the data home, never the install/code tree
(audit T11, 2026-07-16). The old DEFAULT_LOG_DIR = <repo_root>/logs was a
runtime write into the code tree — read-only under a pip/site-packages install,
rsync'd over by deploys, and exactly what core/runtime_paths isolation exists
to prevent.
"""
from pathlib import Path


def test_log_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_LOG_DIR", str(tmp_path / "mylogs"))
    from core.logging import resolve_log_dir
    assert resolve_log_dir() == tmp_path / "mylogs"


def test_log_dir_defaults_to_data_home_not_install_tree(monkeypatch, tmp_path):
    monkeypatch.delenv("POLYROB_LOG_DIR", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from core.logging import resolve_log_dir
    import core

    d = resolve_log_dir()
    assert d == tmp_path / "logs"
    install_root = Path(core.__file__).resolve().parent.parent
    assert install_root not in d.parents and d != install_root


def test_ensure_log_directory_uses_resolver(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_LOG_DIR", str(tmp_path / "logs"))
    from core.logging import ensure_log_directory
    d = ensure_log_directory()
    assert d == tmp_path / "logs"
    assert d.is_dir()
