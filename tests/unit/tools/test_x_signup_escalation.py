"""X signup escalation — owner ask/notice rail (Task 12, 2026-08-18 plan)."""
import pytest

from tools.x_browser.escalation import EscalationOutcome, escalate_and_wait
from tools.x_browser.signup import Obstacle, SignupPaused


class FakeBoard:
    def __init__(self, fulfilled_after=None):
        self.asks_created = []
        self._fulfilled_after = fulfilled_after
        self._polls = 0

    def create_ask(self, *, user_id, what, why="", **kw):
        self.asks_created.append({"what": what, "why": why})

        class _Goal:
            id = "ask_1"
        return _Goal()

    def asks(self, *, user_id, status=None):
        # Simulate the owner fulfilling after N polls (ask disappears from OPEN).
        self._polls += 1
        if self._fulfilled_after is not None and self._polls >= self._fulfilled_after:
            return []

        class _Goal:
            id = "ask_1"
            title = "signup"
        return [_Goal()]


class FakeDriver:
    def __init__(self, cleared_after=1):
        self._cleared_after = cleared_after
        self._probes = 0

    async def challenge_cleared(self):
        self._probes += 1
        return self._probes >= self._cleared_after


@pytest.fixture()
def notices(monkeypatch):
    sent = []

    async def _fake_deliver(container, user_id, text, **kw):
        sent.append(text)
        return "sent"

    monkeypatch.setattr("tools.x_browser.escalation.deliver_user_message",
                        _fake_deliver)
    return sent


def _paused(obstacle):
    return SignupPaused(obstacle, "prompt text", resume_token="u1")


@pytest.mark.asyncio
async def test_blocked_aborts_with_one_notice(notices):
    board = FakeBoard()
    out = await escalate_and_wait(
        None, "u1", "s1", _paused(Obstacle.BLOCKED),
        timeout_sec=1, board=board, poll_interval=0.01)
    assert out is EscalationOutcome.ABORTED
    assert len(notices) == 1
    assert not board.asks_created  # blocked never asks, just notifies


@pytest.mark.asyncio
async def test_interactive_challenge_headed_clears(notices):
    board = FakeBoard()
    out = await escalate_and_wait(
        None, "u1", "s1", _paused(Obstacle.INTERACTIVE_CHALLENGE),
        timeout_sec=1, board=board, driver=FakeDriver(cleared_after=1),
        poll_interval=0.01)
    assert out is EscalationOutcome.CLEARED
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_interactive_challenge_headless_pauses_with_resume_cmd(notices):
    board = FakeBoard()
    out = await escalate_and_wait(
        None, "u1", "s1", _paused(Obstacle.INTERACTIVE_CHALLENGE),
        timeout_sec=1, board=board, driver=None, poll_interval=0.01)
    assert out is EscalationOutcome.PAUSED
    assert any("x-account signup --resume" in n for n in notices)


@pytest.mark.asyncio
async def test_phone_ask_fulfilled_clears(notices):
    board = FakeBoard(fulfilled_after=2)
    out = await escalate_and_wait(
        None, "u1", "s1", _paused(Obstacle.PHONE_REQUIRED),
        timeout_sec=5, board=board, poll_interval=0.01)
    assert out is EscalationOutcome.CLEARED
    assert len(board.asks_created) == 1


@pytest.mark.asyncio
async def test_value_needed_times_out_to_paused(notices):
    board = FakeBoard(fulfilled_after=None)  # owner never fulfills
    out = await escalate_and_wait(
        None, "u1", "s1", _paused(Obstacle.VALUE_NEEDED),
        timeout_sec=0.05, board=board, poll_interval=0.01)
    assert out is EscalationOutcome.PAUSED
    assert len(board.asks_created) == 1
