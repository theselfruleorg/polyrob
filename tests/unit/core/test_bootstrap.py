"""Tests for core/bootstrap.py — container construction extracted from FastAPI lifespan."""

import pytest
import os


def test_build_container_is_importable():
    """bootstrap module exposes build_container."""
    from core.bootstrap import build_container
    assert callable(build_container)


def test_load_env_is_importable():
    """bootstrap module exposes load_env."""
    from core.bootstrap import load_env
    assert callable(load_env)


def test_load_env_loads_dotenv(tmp_path, monkeypatch):
    """load_env loads the correct .env file based on env parameter."""
    env_file = tmp_path / ".env.test"
    env_file.write_text("BOOTSTRAP_TEST_VAR=hello_from_test\n")

    monkeypatch.setenv("ENV", "test")

    from core.bootstrap import load_env
    load_env(env="test", config_dir=str(tmp_path))

    assert os.environ.get("BOOTSTRAP_TEST_VAR") == "hello_from_test"


def test_load_env_local_overrides_env(tmp_path, monkeypatch):
    """config/.env.{env}.local overrides config/.env.{env}."""
    env_file = tmp_path / ".env.test"
    env_file.write_text("LAYER_TEST_VAR=from_env\n")

    local_file = tmp_path / ".env.test.local"
    local_file.write_text("LAYER_TEST_VAR=from_local\n")

    from core.bootstrap import load_env
    load_env(env="test", config_dir=str(tmp_path))

    assert os.environ.get("LAYER_TEST_VAR") == "from_local"


def test_load_env_local_skips_an_unreadable_project_candidate(tmp_path, monkeypatch):
    """The local-mode ladder probes ``./.polyrob/.env`` in the cwd. Run as the
    service user from inside root's 0700 clone (prod 2026-09-20) that probe
    raised PermissionError and killed `polyrob owner asks`. A candidate the
    process cannot even stat is somebody else's file: skip it, keep loading."""
    from pathlib import Path
    from core.bootstrap import load_env
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    (home / ".polyrob" / ".env").write_text("BOOTSTRAP_T_UNREADABLE=from_home\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("POLYROB_HOME", str(home / ".polyrob"))
    monkeypatch.delenv("BOOTSTRAP_T_UNREADABLE", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    real_exists = Path.exists

    def boom(self, *a, **k):
        if self.name == ".env" and self.parent == proj / ".polyrob":
            raise PermissionError(13, "Permission denied", str(self))
        return real_exists(self, *a, **k)

    monkeypatch.setattr(Path, "exists", boom)
    load_env(env="test", local_mode=True, config_dir=str(tmp_path / "nocfg"))   # must not raise
    assert os.environ.get("BOOTSTRAP_T_UNREADABLE") == "from_home"            # the ladder continued
