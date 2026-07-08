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


# --- empty-pipeline escalation (§7.2 tail — the dead-code wiring) -------------

@pytest.mark.asyncio
async def test_empty_pipeline_escalates_when_enabled(monkeypatch):
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "true")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    sink = _FakeSink()
    agent = _FakeAgent(_FakeContainer(sink))
    sent = await escalation.maybe_escalate_empty_pipeline(
        agent, objective_title="Grow the POLYROB following on X",
        planner_summary="blocked: I need Twitter write access from you")
    assert sent is True
    assert sink.sent
    msg = sink.sent[0][1]
    assert "Grow the POLYROB following on X" in msg
    assert "Twitter write access" in msg  # the planner's concrete ask rides along


@pytest.mark.asyncio
async def test_empty_pipeline_noop_when_flag_off(monkeypatch):
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "false")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    sink = _FakeSink()
    agent = _FakeAgent(_FakeContainer(sink))
    assert await escalation.maybe_escalate_empty_pipeline(
        agent, objective_title="x") is False
    assert sink.sent == []


@pytest.mark.asyncio
async def test_empty_pipeline_skips_queue_healthy(monkeypatch):
    # "queue healthy, nothing to add" is the planner's legitimate NON-blocker
    # outcome — it must never turn into an owner ping.
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "true")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    sink = _FakeSink()
    agent = _FakeAgent(_FakeContainer(sink))
    assert await escalation.maybe_escalate_empty_pipeline(
        agent, objective_title="x",
        planner_summary="Queue healthy, nothing to add.") is False
    assert sink.sent == []


@pytest.mark.asyncio
async def test_empty_pipeline_failopen_no_container(monkeypatch):
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "true")

    class _Bare:
        container = None

    assert await escalation.maybe_escalate_empty_pipeline(_Bare(), objective_title="x") is False


# --- flag default ------------------------------------------------------------

def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("GOAL_BLOCKER_ESCALATION", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("AUTONOMY_POSTURE", raising=False)
    from agents.task.constants import AutonomyConfig
    assert AutonomyConfig.goal_blocker_escalation() is False


# --- T2-03/T4-04: the durable ask is created regardless of the push flag ------

@pytest.mark.asyncio
async def test_blocked_goal_creates_ask_even_when_push_flag_off(tmp_path, monkeypatch):
    """The ask-row creation was gated on the same flag as the owner PUSH, so with the
    default OFF a blocked goal left NO ask and `owner fulfill` had nothing to consume.
    Now the ask is always created (silent + durable); only the push stays gated."""
    monkeypatch.delenv("GOAL_BLOCKER_ESCALATION", raising=False)  # push OFF
    monkeypatch.delenv("AUTONOMY_POSTURE", raising=False)         # silent posture
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    from agents.task.goals.board import GoalBoard, ASK_OPEN, STATUS_BLOCKED
    from agents.task.goals.dispatcher import GoalDispatcher

    board = GoalBoard(str(tmp_path / "g.db"))
    g = board.create(user_id="rob", title="Post the announcement")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="needs twitter write access")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="needs twitter write access")  # trips breaker
    assert board.get(g.id).status == STATUS_BLOCKED

    class _Agent:
        container = None  # no push target; the ask must still be created

    disp = GoalDispatcher(board, _Agent())
    await disp._maybe_escalate_blocked(board.get(g.id))

    asks = board.asks(user_id="rob", status=ASK_OPEN)
    assert asks, "a blocked goal must leave a durable ask even with the push flag off"
    assert any("Unblock goal" in a.title for a in asks)
