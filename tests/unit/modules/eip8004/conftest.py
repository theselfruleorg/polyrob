import pytest


@pytest.fixture(autouse=True)
def isolate_feedback_replays(tmp_path, monkeypatch):
    monkeypatch.setattr("modules.eip8004.feedback_replays.sidecar_db_path",
                        lambda name: tmp_path / name)
