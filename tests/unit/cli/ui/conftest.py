"""Terminal input tests must never write the developer's real command history, nor the
developer's real model defaults: a REPL `/model <name>` saves and `/model reset` clears
``cli.json``, the home ``.env`` and ``model.json`` (2026-11-22: an unisolated reset test
removed DEFAULT_PROVIDER/DEFAULT_MODEL from a developer's ``~/.polyrob/.env``)."""
import pytest


@pytest.fixture(autouse=True)
def isolated_input_history(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.ui.app.default_history_path", lambda: tmp_path / "history")


@pytest.fixture(autouse=True)
def isolated_model_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(tmp_path / "cli.json"))
    monkeypatch.setattr("cli.model_default._env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr("core.runtime_paths.prefs_home_dir", lambda: str(tmp_path / "prefs"))
