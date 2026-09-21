"""057: one answer to 'is this session autonomous', and the disclosure gate."""
import pytest

from agents.task.goals.autonomy_marker import mark_autonomous
from agents.task.session_class import is_autonomous_session, tool_disclosure_enabled


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch):
    monkeypatch.delenv("TOOL_PROGRESSIVE_DISCLOSURE", raising=False)
    monkeypatch.delenv("AUTONOMOUS_TOOL_DISCLOSURE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)


def test_unmarked_session_is_interactive():
    assert is_autonomous_session("never-marked") is False
    assert is_autonomous_session(None) is False
    assert is_autonomous_session("") is False


def test_marked_session_is_autonomous():
    mark_autonomous("sc-goal-1", "g1")
    assert is_autonomous_session("sc-goal-1") is True


def test_probe_error_fails_open_to_interactive(monkeypatch):
    import agents.task.goals.autonomy_marker as marker

    def _boom(_sid):
        raise RuntimeError("registry gone")

    monkeypatch.setattr(marker, "is_autonomous", _boom)
    assert is_autonomous_session("sc-goal-1") is False


def test_disclosure_is_off_by_default():
    mark_autonomous("sc-goal-2", "g2")
    assert tool_disclosure_enabled("sc-goal-2") is False
    assert tool_disclosure_enabled(None) is False


def test_the_existing_flag_still_turns_it_on_everywhere(monkeypatch):
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    assert tool_disclosure_enabled(None) is True


def test_autonomous_key_applies_only_to_autonomous_sessions(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_TOOL_DISCLOSURE", "true")
    mark_autonomous("sc-goal-3", "g3")
    assert tool_disclosure_enabled("sc-goal-3") is True
    assert tool_disclosure_enabled("an-owner-chat") is False
