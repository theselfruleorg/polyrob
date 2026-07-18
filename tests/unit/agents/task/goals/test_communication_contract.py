"""§3.3 the communication contract + §3.4 framework notices demoted to a safety net.

The agent owns keeping its user informed (a cache-stable system-prompt block in
autonomous sessions); framework-composed pushes shrink to events the agent
cannot report itself — the terminal completion push fires ONLY when the agent
said nothing (RunOutcome.user_messages empty), and the blocker-escalation push
only when the agent itself didn't report the block. Durable asks stay
unconditional.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agents.task.goals.board import Goal, STATUS_BLOCKED, STATUS_READY
from agents.task.goals.dispatcher import GoalDispatcher
from tools.controller.types import ActionResult


# ---------------------------------------------------------------------------
# §3.3 — the contract block in the system prompt (autonomous sessions only)
# ---------------------------------------------------------------------------

def test_system_prompt_contract_block_for_autonomous_sessions():
    from agents.task.agent.prompts import SystemPrompt
    p = SystemPrompt("actions...", autonomous=True)
    text = p.get_system_message().content
    assert "<communication-contract>" in text
    assert "send_message" in text
    assert "goal_show" in text
    assert "never claim delivered work" in text.lower()


def test_system_prompt_no_contract_block_for_interactive_sessions():
    from agents.task.agent.prompts import SystemPrompt
    p = SystemPrompt("actions...")
    assert "<communication-contract>" not in p.get_system_message().content


def test_run_task_to_outcome_marks_autonomous_before_construction():
    """The marker must be visible DURING create_session (agent/prompt build),
    not only after — the contract block gates on it at prompt-build time."""
    from agents.task.runtime.run_as_session import run_task_to_outcome
    from agents.task.goals.autonomy_marker import is_autonomous, _SESSIONS
    _SESSIONS.clear()
    seen = {}

    class _TA:
        async def create_session(self, *, user_id, request, session_id=None, **kw):
            seen["session_id"] = session_id
            seen["marked_during_create"] = bool(session_id) and is_autonomous(session_id)
            return {"id": session_id or "sess-x"}

        async def run_session(self, user_id, session_id):
            return "Session completed successfully"

        def _extract_chat_reply(self, sid):
            return "done"

    try:
        asyncio.run(run_task_to_outcome(_TA(), user_id="u1", request={"task": "t"},
                                        autonomous=True))
        assert seen["session_id"], "autonomous runs pre-generate the session id"
        assert seen["marked_during_create"] is True
    finally:
        _SESSIONS.clear()


# ---------------------------------------------------------------------------
# §3.4 — terminal completion push is a FALLBACK (agent said nothing)
# ---------------------------------------------------------------------------

def _done_step(text):
    return SimpleNamespace(
        model_output=SimpleNamespace(
            action=[SimpleNamespace(model_dump=lambda exclude_unset=True: {"done": {"text": text}})]),
        result=[ActionResult(is_done=True, extracted_content=text)])


def _send_step(text):
    return SimpleNamespace(
        model_output=SimpleNamespace(
            action=[SimpleNamespace(model_dump=lambda exclude_unset=True: {"send_message": {"text": text}})]),
        result=[ActionResult(extracted_content="Message sent to user (non-blocking)")])


def _ta(steps):
    agent = SimpleNamespace(
        history=SimpleNamespace(history=list(steps)),
        _is_sub_agent=False, _last_result=steps[-1].result,
        state=SimpleNamespace(n_steps=2, session_created_at=None))
    orch = SimpleNamespace(agents={"m": agent}, session_id="s1", usage_tracker=None)

    class _TA:
        container = None

        async def create_session(self, *, user_id, request, session_id=None, **kw):
            return {"id": session_id or "s1"}

        async def run_session(self, user_id, session_id):
            return "Session completed successfully"

        def get_orchestrator(self, sid):
            return orch

        def _extract_chat_reply(self, sid):
            return ""

        deliver_self_wake = None

    return _TA()


class _Board:
    def __init__(self):
        self.successes, self.failures = [], []

    def record_success(self, gid, session_id=None, result=None):
        self.successes.append(gid)

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error))
        return Goal(id=gid, user_id="u1", title="t", status=STATUS_READY)

    def create_ask(self, **kw):
        return None

    def get(self, gid):
        return Goal(id=gid, user_id="u1", title="t", status="done")

    def set_outcome(self, gid, outcome):
        return True

    def block_from_ready(self, gid, *, error):
        return True


def test_no_completion_push_when_agent_reported(monkeypatch):
    monkeypatch.setenv("GOAL_NOTIFY_ON_DONE", "true")
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "false")
    pushed = []

    async def _push(goal, session_id, final, **kw):
        pushed.append(final)

    ta = _ta([_send_step("progress: wrote the report to workspace/report.md"),
              _done_step("OUTCOME: workspace/report.md")])
    board = _Board()
    disp = GoalDispatcher(board, ta)
    monkeypatch.setattr(disp, "_notify_owner_done", _push)
    asyncio.run(disp._run_goal(Goal(id="g1", user_id="u1", title="write report")))
    assert board.successes == ["g1"]
    assert pushed == [], ("the agent already told its user — the framework must "
                          "not push a second, framework-composed message")


def test_fallback_completion_push_when_agent_silent(monkeypatch):
    monkeypatch.setenv("GOAL_NOTIFY_ON_DONE", "true")
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "false")
    pushed = []

    async def _push(goal, session_id, final, **kw):
        pushed.append(final)

    ta = _ta([_done_step("OUTCOME: workspace/report.md")])  # no send_message
    board = _Board()
    disp = GoalDispatcher(board, ta)
    monkeypatch.setattr(disp, "_notify_owner_done", _push)
    asyncio.run(disp._run_goal(Goal(id="g2", user_id="u1", title="write report")))
    assert pushed, "agent said nothing — the safety net fires"


# ---------------------------------------------------------------------------
# §3.4 — blocker-escalation push only when the agent didn't report the block
# ---------------------------------------------------------------------------

def test_no_escalation_push_when_agent_reported_blocker(monkeypatch):
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "true")
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "false")
    escalated = []

    async def _esc(*a, **k):
        escalated.append(1)
        return True

    import agents.task.goals.escalation as esc_mod
    monkeypatch.setattr(esc_mod, "maybe_escalate_blocked", _esc)

    ta = _ta([_send_step("blocker: x402 store unavailable — need database service"),
              _done_step("OUTCOME: BLOCKED — x402 store unavailable")])
    board = _Board()
    board_status = {"status": STATUS_BLOCKED}

    def _rf(gid, error=None, session_id=None):
        board.failures.append((gid, error))
        return Goal(id=gid, user_id="u1", title="t", status=STATUS_BLOCKED)

    board.record_failure = _rf
    disp = GoalDispatcher(board, ta)
    # Title deliberately avoids goal_tools._TOOL_TEXT_TOKENS keywords (e.g. "invoice" ->
    # x402_invoice): a matched keyword makes _resolve_tools infer a money tool, which trips
    # the UNRELATED §6.2 unmetered-money pre-flight gate before the run even starts — that
    # gate's own early-exit calls _maybe_escalate_blocked() with agent_reported defaulted to
    # False, short-circuiting the run.blocked/user_messages path this test means to exercise.
    asyncio.run(disp._run_goal(Goal(id="g3", user_id="u1", title="process task")))
    assert escalated == [], ("the agent's own blocker report reached the user — "
                             "no duplicate framework escalation push")


def test_escalation_push_when_agent_silent_about_block(monkeypatch):
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "true")
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "false")
    escalated = []

    async def _esc(*a, **k):
        escalated.append(1)
        return True

    import agents.task.goals.escalation as esc_mod
    monkeypatch.setattr(esc_mod, "maybe_escalate_blocked", _esc)

    ta = _ta([_done_step("OUTCOME: BLOCKED — x402 store unavailable")])  # silent
    board = _Board()

    def _rf(gid, error=None, session_id=None):
        board.failures.append((gid, error))
        return Goal(id=gid, user_id="u1", title="t", status=STATUS_BLOCKED)

    board.record_failure = _rf
    disp = GoalDispatcher(board, ta)
    # Same title fix as g3 above — avoid accidental money-tool keyword inference.
    asyncio.run(disp._run_goal(Goal(id="g4", user_id="u1", title="process task")))
    assert escalated, "agent said nothing about the block — safety net fires"
