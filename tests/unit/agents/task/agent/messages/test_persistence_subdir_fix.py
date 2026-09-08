"""Regression: load_from_disk must call create_file_path with subdir_name= (not subdir=)."""
from pathlib import Path

from agents.task.agent.messages.persistence import PersistenceMixin


class _Host(PersistenceMixin):
    def __init__(self):
        import logging
        self.logger = logging.getLogger("test_persistence_subdir_fix")
        self.session_id = "sess_1"


def test_load_from_disk_uses_subdir_name_kwarg(monkeypatch):
    captured = {}

    class _PM:
        def create_file_path(self, **kwargs):
            captured.update(kwargs)
            return Path("/nonexistent/message_history.json")  # .exists() -> False, early return

    monkeypatch.setattr("agents.task.path.pm", lambda: _PM())

    host = _Host()
    # Must NOT raise TypeError (the bug); returns False because the path doesn't exist.
    assert host.load_from_disk("sess_1") is False
    assert "subdir_name" in captured
    assert "subdir" not in captured
