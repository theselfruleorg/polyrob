"""Terminal input tests must never write the developer's real command history."""
import pytest


@pytest.fixture(autouse=True)
def isolated_input_history(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.ui.app.default_history_path", lambda: tmp_path / "history")
