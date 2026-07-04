"""P9 pass-2 — HistoryIOMixin extracted from service.py."""
from agents.task.agent.core.history_io import HistoryIOMixin


def test_agent_composes_history_io_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, HistoryIOMixin)
    # methods resolve to the mixin, not a leftover copy on Agent
    for m in ("save_history", "create_history_gif", "get_conversation_screenshots", "save_screenshot"):
        assert getattr(Agent, m).__qualname__.startswith("HistoryIOMixin")


class _Host(HistoryIOMixin):
    pass


def test_save_history_delegates_to_history():
    class _FakeHistory:
        def __init__(self):
            self.saved = None

        def save_to_file(self, path):
            self.saved = path

    h = _Host()
    h.history = _FakeHistory()
    h.save_history("AgentHistory.json")
    assert h.history.saved == "AgentHistory.json"


def test_save_screenshot_empty_returns_none():
    assert _Host().save_screenshot("s1", "", step_number=0) is None
