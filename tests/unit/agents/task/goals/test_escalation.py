"""Blocker → owner escalation (§7.2). When a goal trips the circuit breaker it must
no longer die silently — it surfaces a concrete ask to the owner (gated, fail-open)."""
import pytest

from agents.task.goals import escalation
from agents.task.goals.board import Goal, STATUS_BLOCKED, STATUS_READY


def _blocked_goal():
    return Goal(id="g1", user_id="rob", title="Draft enterprise outreach asset",
                status=STATUS_BLOCKED, consecutive_failures=2, max_retries=2,
                last_failure_error="run did not complete (refusal or empty)")


# --- message builder ---------------------------------------------------------

def test_build_blocker_message_names_goal_and_reason():
    msg = escalation.build_blocker_escalation(_blocked_goal())
    assert "Draft enterprise outreach asset" in msg
    assert "refusal or empty" in msg
    assert "blocked" in msg.lower()


def test_build_blocker_message_asks_for_help():
    msg = escalation.build_blocker_escalation(_blocked_goal())
    # it should be an ASK, not just a status line
    assert "?" in msg or "need" in msg.lower() or "help" in msg.lower()


# --- gated push --------------------------------------------------------------

class _FakeSink:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class _FakeContainer:
    def __init__(self, sink):
        self._sink = sink

    def get_service(self, name):
        return self._sink if name in ("telegram_sink", "message_router") else None


class _FakeAgent:
    def __init__(self, container):
        self.container = container


@pytest.mark.asyncio
async def test_escalate_only_when_blocked(monkeypatch):
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "true")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    sink = _FakeSink()
    agent = _FakeAgent(_FakeContainer(sink))
    # a non-blocked (ready) goal must NOT escalate
    ready = Goal(id="g2", user_id="rob", title="x", status=STATUS_READY)
    assert await escalation.maybe_escalate_blocked(agent, ready) is False
    assert sink.sent == []
    # a blocked goal DOES escalate
    assert await escalation.maybe_escalate_blocked(agent, _blocked_goal()) is True
    assert sink.sent and "Draft enterprise outreach asset" in sink.sent[0][1]


@pytest.mark.asyncio
async def test_escalate_noop_when_flag_off(monkeypatch):
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "false")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    sink = _FakeSink()
    agent = _FakeAgent(_FakeContainer(sink))
    assert await escalation.maybe_escalate_blocked(agent, _blocked_goal()) is False
    assert sink.sent == []


@pytest.mark.asyncio
async def test_escalate_failopen_no_container(monkeypatch):
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "true")

    class _Bare:
        container = None

    assert await escalation.maybe_escalate_blocked(_Bare(), _blocked_goal()) is False


# --- flag default ------------------------------------------------------------

def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("GOAL_BLOCKER_ESCALATION", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    from agents.task.constants import AutonomyConfig
    assert AutonomyConfig.goal_blocker_escalation() is False
