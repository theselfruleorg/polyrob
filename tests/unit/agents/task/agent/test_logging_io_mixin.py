"""P9 pass-3 — LoggingIOMixin extracted from service.py."""
from agents.task.agent.core.logging_io import LoggingIOMixin


def test_agent_composes_logging_io_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, LoggingIOMixin)
    for m in ("_log_response", "_log_context_breakdown", "_save_conversation",
              "_write_messages_to_file", "_write_response_to_file", "_log_agent_run",
              "_log_tool_outputs"):
        assert getattr(Agent, m).__qualname__.startswith("LoggingIOMixin")


class _Host(LoggingIOMixin):
    pass


def test_save_conversation_noop_without_path():
    h = _Host()
    h.save_conversation_path = None
    # must return without touching disk
    h._save_conversation([], object())


def test_log_tool_outputs_noop_without_path():
    h = _Host()
    h.tool_output_log_path = None
    h._log_tool_outputs([], [], 0)  # returns early, no raise
