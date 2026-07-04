"""Tests for core/paths.py::polyrob_home — the framework config-HOME seam."""

from pathlib import Path

from core.paths import polyrob_home


def test_default_is_dot_polyrob_under_home(monkeypatch):
    monkeypatch.delenv("POLYROB_HOME", raising=False)
    assert polyrob_home() == Path.home() / ".polyrob"


def test_polyrob_home_env_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "custom_home"
    monkeypatch.setenv("POLYROB_HOME", str(target))
    assert polyrob_home() == target


def test_returns_a_path(monkeypatch):
    monkeypatch.delenv("POLYROB_HOME", raising=False)
    assert isinstance(polyrob_home(), Path)
