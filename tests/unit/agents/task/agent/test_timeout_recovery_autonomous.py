"""An LLM timeout inside an AUTONOMOUS run never DMs the owner.

Prod 2026-09-21 07:05Z (and 09-20 ~16:xx): the daily meta-research cron hit a
138 s LLM timeout, the recovery path built a ``send_message`` — "⚠️ The AI took
too long to respond … Please send another message to retry" — and the cron
delivery rail carried it to the owner's Telegram. That text is written for an
interactive user who can retry; in a cron/goal run there is no such user, the
loop itself retries on the next step, and the owner just receives noise.
"""
import logging
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agents.task.agent.core.llm_runner import LLMRunnerMixin
from tools.controller.registry.views import ActionModel


class _Send(BaseModel):
    text: str


class _Done(BaseModel):
    text: str


class _ActionModel(ActionModel):
    # The registry builds this dynamically in prod; a static subclass of the
    # real base keeps AgentOutput's validator happy.
    send_message: _Send | None = None
    done: _Done | None = None


class _Controller:
    def create_action_model(self):
        return _ActionModel

    def get_action_names(self):
        return ["send_message", "done", "read_file"]


class _Runner(LLMRunnerMixin):
    def __init__(self, session_id):
        self.session_id = session_id
        self.controller = _Controller()
        self.state = SimpleNamespace(n_steps=4)
        self.logger = logging.getLogger("t")


def test_interactive_session_still_notifies_the_user(monkeypatch):
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: False)
    out = _Runner("s-interactive")._timeout_recovery_output(138.0)
    assert len(out.action) == 1
    assert out.action[0].send_message is not None
    assert "too long" in out.action[0].send_message.text


def test_autonomous_session_never_sends_a_message(monkeypatch):
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: True)
    out = _Runner("s-cron")._timeout_recovery_output(138.0)
    assert out.action == [], "no send_message and no done — the next step retries"
    assert "138" in out.current_state.memory
    assert "retry" in out.current_state.next_goal.lower()
    assert "await guidance" not in out.current_state.next_goal.lower()


def test_autonomous_detection_failure_is_fail_open_to_interactive(monkeypatch):
    def boom(sid):
        raise RuntimeError("marker unreadable")
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", boom)
    out = _Runner("s-x")._timeout_recovery_output(60.0)
    assert len(out.action) == 1 and out.action[0].send_message is not None
