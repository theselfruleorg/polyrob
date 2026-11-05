"""102 M-C: one logger per component, and the log line still names the session."""
import logging

from agents.task.logging_config import get_task_logger
from cli.ui.log_squelch import _short_name


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_a_session_adds_no_logger_to_the_process():
    get_task_logger("sessioncomponent", "first-id")
    before = len(logging.root.manager.loggerDict)
    for i in range(20):
        get_task_logger("sessioncomponent", f"session-{i}")
    assert len(logging.root.manager.loggerDict) == before


def test_the_record_name_stays_the_session_form_and_the_message_is_unchanged():
    capture = _Capture()
    base = get_task_logger("sessioncomponent")
    base.addHandler(capture)
    try:
        logger = get_task_logger("sessioncomponent", "abcd1234-rest-of-the-id")
        logger.warning("hello %s", "world", extra={"mine": 1})
        base.warning("no session")
    finally:
        base.removeHandler(capture)
    with_session, without = capture.records
    assert with_session.name == "task.sessioncomponent[abcd1234]"
    assert _short_name(with_session.name) == "sessioncomponent[abcd1234]"
    assert with_session.getMessage() == "hello world"
    assert with_session.mine == 1
    assert without.name == "task.sessioncomponent"


def test_the_adapter_has_the_calls_the_agent_uses():
    logger = get_task_logger("sessioncomponent", "abcd1234")
    logger.setLevel(logging.INFO)
    assert logger.isEnabledFor(logging.INFO)
    assert logger.getEffectiveLevel() == logging.INFO
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")


def test_a_child_id_is_on_the_record_and_not_in_a_logger_name():
    """A unique name per sub-agent stays in the logging manager for the process life."""
    import logging

    from agents.task.logging_config import get_task_logger
    before = set(logging.root.manager.loggerDict)
    first = get_task_logger("sub_agent", "session-1", child="sub-1-abc123")
    second = get_task_logger("sub_agent", "session-1", child="sub-2-def456")
    assert first.logger is second.logger
    assert not [n for n in set(logging.root.manager.loggerDict) - before if "sub-" in n]
    assert first.extra["task_session"] == "session-/sub-1-abc123"
