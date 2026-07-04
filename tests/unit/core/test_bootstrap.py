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
