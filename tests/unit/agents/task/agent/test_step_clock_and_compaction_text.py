"""079 W7 (R8) — text drift in the per-step state message and the context log.

* The step clock was ``datetime.now()`` with no zone: on a host whose local zone
  is not UTC the agent read a wall-clock it could not place, and compared it to
  UTC timestamps everywhere else. It now says UTC, and is UTC.
* ``_log_context_breakdown`` said "will compact at 65%" while the ONE ladder
  (``agents/task/constants.py``) is 70/85/95. The numbers now come from the ladder.
"""
import logging
import re
from datetime import datetime, timezone

from agents.task.agent.core.logging_io import LoggingIOMixin
from agents.task.agent.prompts import AgentMessagePrompt
from agents.task.constants import COMPACTION_LLM_PCT, COMPACTION_WARN_PCT


class _Tree:
    def clickable_elements_to_string(self, include_attributes=None):
        return ""


class _State:
    def __init__(self):
        self.url = ""
        self.title = "No Browser"
        self.tabs = []
        self.screenshot = None
        self.pixels_above = 0
        self.pixels_below = 0
        self.element_tree = _Tree()


def test_the_step_clock_is_labelled_and_computed_in_utc():
    text = AgentMessagePrompt(state=_State(), result=[]).get_user_message(use_vision=False).content
    m = re.search(r"Current date and time: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC", text)
    assert m, text[:400]
    shown = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - shown).total_seconds()) < 120


class _MM:
    def __init__(self, pct):
        self.pct = pct

    def context_usage(self):
        return {"total_tokens": 1000, "limit": 2000, "pct": self.pct, "source": "provider_usage",
                "slots": {k: 0 for k in ("foundation", "hmem", "conversation", "ephemeral",
                                         "tool_schemas")}}


class _State2:
    n_steps = 3


class _Host(LoggingIOMixin):
    def __init__(self, pct):
        self.message_manager = _MM(pct)
        self.state = _State2()
        self.logger = logging.getLogger("test.logging_io.ladder")


def _warnings(caplog, pct):
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="test.logging_io.ladder"):
        _Host(pct)._log_context_breakdown()
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_the_compaction_warning_names_the_ladder_not_a_literal(caplog):
    warned = _warnings(caplog, COMPACTION_WARN_PCT + 1)
    assert warned and f"will compact at {COMPACTION_LLM_PCT}%" in warned[0]
    assert "65%" not in warned[0]


def test_no_warning_under_the_warn_band(caplog):
    assert _warnings(caplog, COMPACTION_WARN_PCT - 1) == []
