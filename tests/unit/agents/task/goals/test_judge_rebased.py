"""§4.3 — evidence-grounded completion review for autonomous runs.

The judge reads the CLAIM (done_text) against the mechanical evidence pack —
ledger, artifact diff, captured ids — no longer requiring a `payload.acceptance`
to run. Disposition: met → verified; unmet (claim contradicts evidence) →
failure with the specific gap; unclear/error → done (unverified) — completes,
honestly labeled, and the learning loops (self-wake, skill distillation) do
NOT consume it. Interactive chat is untouched.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from agents.task.goals.board import Goal, STATUS_READY
from agents.task.goals.dispatcher import GoalDispatcher
from tools.controller.types import ActionResult


class _JudgeLLM:
    def __init__(self, verdict, reason="because"):
        self._verdict = verdict
        self.calls = []

    async def ainvoke(self, msgs):
        self.calls.append(msgs)
        return SimpleNamespace(content=json.dumps(
            {"verdict": self._verdict, "reason": "because"}))


def _orch(done_text, judge_llm):
    done = ActionResult(is_done=True, extracted_content=done_text)
    agent = SimpleNamespace(
        history=SimpleNamespace(history=[SimpleNamespace(
            model_output=SimpleNamespace(
                action=[SimpleNamespace(model_dump=lambda exclude_unset=True: {"done": {}})]),
            result=[done])]),
        _is_sub_agent=False, _last_result=[done],
        state=SimpleNamespace(n_steps=3, session_created_at=None),
        _judge_llm=judge_llm, llm=judge_llm, agent_id="a1",
    )
    return SimpleNamespace(agents={"m": agent}, session_id="s1", usage_tracker=None)


class _Board:
    def __init__(self):
        self.failures, self.successes = [], []

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error))
        return Goal(id=gid, user_id="u1", title="t", status=STATUS_READY)

    def record_success(self, gid, session_id=None, result=None):
        self.successes.append(gid)

    def create_ask(self, **kw):
        return None

    def get(self, gid):
        return Goal(id=gid, user_id="u1", title="t", status="done")

    def set_outcome(self, gid, outcome):
        return True

    def block_from_ready(self, gid, *, error):
        return True


def _task_agent(orch):
    class _TA:
        container = None

        async def create_session(self, *, user_id, request):
            return {"id": "s1"}

        async def run_session(self, user_id, session_id):
            return "Session completed successfully"

        def get_orchestrator(self, sid):
            return orch

        def _extract_chat_reply(self, sid):
            return ""

        deliver_self_wake = None

    return _TA()


def _run_goal(disp, goal):
    asyncio.run(disp._run_goal(goal))


def _mk(verdict, monkeypatch, *, self_wake_on=False):
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "true")
    if self_wake_on:
        monkeypatch.setenv("GOAL_SELF_WAKE_ENABLED", "true")
    llm = _JudgeLLM(verdict)
    orch = _orch("Posted the thread!\nOUTCOME: https://x.com/rob/status/1", llm)
    board = _Board()
    disp = GoalDispatcher(board, _task_agent(orch))
    return llm, board, disp


def test_judge_runs_without_acceptance_and_met_verifies(monkeypatch):
    llm, board, disp = _mk("met", monkeypatch)
    goal = Goal(id="g1", user_id="u1", title="post the thread")  # NO acceptance
    _run_goal(disp, goal)
    assert llm.calls, "the judge must run for an autonomous completion even without acceptance"
    assert board.successes == ["g1"]


def test_unmet_claim_contradicting_evidence_fails_run(monkeypatch):
    llm, board, disp = _mk("unmet", monkeypatch)
    goal = Goal(id="g2", user_id="u1", title="post the thread")
    _run_goal(disp, goal)
    assert not board.successes
    assert board.failures and "completion judge" in board.failures[0][1]


def test_unclear_completes_as_unverified_and_skips_self_wake(monkeypatch):
    llm, board, disp = _mk("unclear", monkeypatch, self_wake_on=True)
    woke = []

    async def _wake(goal, session_id, final):
        woke.append(goal.id)

    monkeypatch.setattr(disp, "_self_wake", _wake)
    goal = Goal(id="g3", user_id="u1", title="post the thread")
    _run_goal(disp, goal)
    assert board.successes == ["g3"], "unclear must still complete (framework for arbitrary goals)"
    assert woke == [], "an UNVERIFIED completion must not feed self-wake"


def test_verified_completion_feeds_self_wake(monkeypatch):
    llm, board, disp = _mk("met", monkeypatch, self_wake_on=True)
    woke = []

    async def _wake(goal, session_id, final):
        woke.append(goal.id)

    monkeypatch.setattr(disp, "_self_wake", _wake)
    goal = Goal(id="g4", user_id="u1", title="post the thread")
    _run_goal(disp, goal)
    assert woke == ["g4"]


def test_unverified_notify_label_is_honest(monkeypatch):
    llm, board, disp = _mk("unclear", monkeypatch)
    monkeypatch.setenv("GOAL_NOTIFY_ON_DONE", "true")
    pushed = []

    async def _push(container, text):
        pushed.append(text)
        return True

    import agents.task.goals.dispatcher as disp_mod
    monkeypatch.setattr("core.self_evolution.push_owner_message", _push)
    goal = Goal(id="g5", user_id="u1", title="post the thread")
    _run_goal(disp, goal)
    assert pushed, "completion still reported"
    assert "unverified" in pushed[0].lower()
    assert "✅" not in pushed[0], "no green checkmark on an unverified claim"


def test_judge_prompt_includes_evidence_pack(monkeypatch):
    llm, board, disp = _mk("met", monkeypatch)
    goal = Goal(id="g6", user_id="u1", title="post the thread")
    _run_goal(disp, goal)
    body = llm.calls[0][-1].content
    assert "EXECUTED ACTIONS" in body
    assert "CLAIM" in body.upper()


def test_interactive_chat_untouched():
    """The evidence judge is an AUTONOMOUS-run disposition — nothing in the
    chat path imports or invokes it (chat keeps today's fail-open behavior)."""
    import inspect
    import agents.task_agent_lite as ta
    src = inspect.getsource(ta.TaskAgent._chat_once_locked)
    assert "judge" not in src.lower()


# --- learning-loop exclusion: no inline skill distillation from autonomous runs ---

def test_background_review_skips_autonomous_sessions(monkeypatch):
    from agents.task.agent.core.background_review import BackgroundReviewMixin
    from agents.task.goals.autonomy_marker import mark_autonomous, _SESSIONS
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "1")
    _SESSIONS.clear()

    agent = SimpleNamespace(session_id="sess-auto", _is_sub_agent=False,
                            _bg_review_productive_turns=0)
    fire = BackgroundReviewMixin._bg_review_should_fire
    try:
        assert fire(agent, turn_was_productive=True) is True, (
            "sanity: a non-autonomous session at interval fires")
        mark_autonomous("sess-auto")
        agent._bg_review_productive_turns = 0
        assert fire(agent, turn_was_productive=True) is False, (
            "an autonomous run must not distil skills inline — only verified "
            "outcomes may compound (§4.3)")
    finally:
        _SESSIONS.clear()
