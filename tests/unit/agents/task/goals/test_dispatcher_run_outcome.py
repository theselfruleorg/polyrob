"""§2 dispatcher consumption of RunOutcome — the live case-study replay.

Goal 58a1385d18bf (2026-07-09): the agent did real work, hit a real bug, exited
honestly with done("OUTCOME: BLOCKED — x402 payment request store unavailable…")
— and the framework recorded SUCCESS with result "Processing actions" because
every consumer re-extracted strings from message history. With RunOutcome the
dispatcher reads the done() text from the action ledger: the run must end
blocked, record_success must NOT fire, and no ✅ push may be sent.
"""
import asyncio
from types import SimpleNamespace

from agents.task.goals.board import Goal, STATUS_BLOCKED, STATUS_READY
from agents.task.goals.dispatcher import GoalDispatcher
from tools.controller.types import ActionResult


LIVE_BLOCKED_TEXT = (
    "OUTCOME: BLOCKED — x402 payment request store unavailable (no database service)"
)


class _Action:
    def __init__(self, name, params=None):
        self._d = {name: params or {}}

    def model_dump(self, exclude_unset=True):
        return dict(self._d)


class _Step:
    def __init__(self, actions, results):
        self.model_output = SimpleNamespace(action=list(actions))
        self.result = list(results)


class _Agent:
    def __init__(self, steps, *, n_steps=1):
        self.history = SimpleNamespace(history=list(steps))
        self._is_sub_agent = False
        self._last_result = steps[-1].result if steps else None
        self.state = SimpleNamespace(n_steps=n_steps)


def _orch_with_done(done_text, *, with_send=True):
    steps = []
    if with_send:
        steps.append(
            _Step([_Action("send_message", {"text": "blocker report: x402 store unavailable"})],
                  [ActionResult(extracted_content="Message sent to user (non-blocking)")]))
    steps.append(
        _Step([_Action("done", {"text": done_text})],
              [ActionResult(is_done=True, extracted_content=done_text)]))
    agent = _Agent(steps)
    return SimpleNamespace(agents={"main": agent}, session_id="s43de", usage_tracker=None)


class _CaseStudyAgent:
    """run_session returns the generic status; the reply extractor is poisoned by
    the P2-16 placeholder — exactly the live shape. The honest signal lives ONLY
    in the action ledger."""

    def __init__(self, done_text=LIVE_BLOCKED_TEXT, *, with_send=True):
        self._orch = _orch_with_done(done_text, with_send=with_send)

    async def create_session(self, *, user_id, request):
        return {"id": "s43de"}

    async def run_session(self, user_id, session_id):
        return "Session completed successfully"

    def get_orchestrator(self, session_id):
        return self._orch

    def _extract_chat_reply(self, session_id):
        return "Processing actions"

    deliver_self_wake = None


class _FakeBoard:
    def __init__(self):
        self.successes, self.failures, self.blocked, self.outcomes = [], [], [], []
        self.results = []
        self._status = "running"

    def record_success(self, gid, session_id=None, result=None):
        self.successes.append(gid)
        self.results.append(result)
        self._status = "done"

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error))
        self._status = STATUS_READY
        return Goal(id=gid, user_id="u1", title="t", status=STATUS_READY)

    def block_from_ready(self, gid, *, error):
        self.blocked.append((gid, error))
        self._status = STATUS_BLOCKED
        return True

    def get(self, gid):
        return Goal(id=gid, user_id="u1", title="t", status=self._status)

    def set_outcome(self, gid, outcome):
        self.outcomes.append((gid, outcome))
        return True

    def create_ask(self, **kw):
        return None


def test_live_case_replay_blocked_done_is_never_success():
    board = _FakeBoard()
    disp = GoalDispatcher(board, _CaseStudyAgent())
    goal = Goal(id="58a1385d18bf", user_id="u1", title="Grow your $10")
    asyncio.run(disp._run_goal(goal))
    assert not board.successes, "an honest BLOCKED done() must never be recorded as success"
    assert board.failures and "x402" in board.failures[0][1]
    assert board.blocked, "agent-declared BLOCKED must block immediately"
    assert board.outcomes and board.outcomes[0][1].startswith("BLOCKED")


def test_live_case_replay_no_completion_push_fires(monkeypatch):
    pushed = []

    async def _push(goal, session_id, final, **kw):
        pushed.append(final)

    board = _FakeBoard()
    disp = GoalDispatcher(board, _CaseStudyAgent())
    monkeypatch.setattr(disp, "_notify_owner_done", _push)
    goal = Goal(id="58a1385d18bf", user_id="u1", title="Grow your $10")
    asyncio.run(disp._run_goal(goal))
    assert pushed == [], "no ✅ completion push may fire for a BLOCKED run"


def test_success_records_honest_done_text_not_placeholder():
    """A genuine completion whose reply extraction degrades to the placeholder
    must record the done() ledger text as the board result."""
    board = _FakeBoard()
    agent = _CaseStudyAgent(done_text="Report written.\nOUTCOME: workspace/report.md")
    disp = GoalDispatcher(board, agent)
    goal = Goal(id="g-honest", user_id="u1", title="write the report")
    asyncio.run(disp._run_goal(goal))
    assert board.successes == ["g-honest"]
    assert board.results and "OUTCOME: workspace/report.md" in board.results[0]
    assert "Processing actions" not in (board.results[0] or "")


def test_done_after_every_action_errored_is_failure():
    """§4.2 NEW invariant: done() where EVERY substantive action errored is not
    a judgment call — it is a failure, never a recorded success."""
    board = _FakeBoard()
    agent = _CaseStudyAgent(done_text="All done, everything went great!")
    failing = _Agent([
        _Step([_Action("x402_request")], [ActionResult(error="store unavailable")]),
        _Step([_Action("filesystem_write_file")], [ActionResult(error="permission denied")]),
        _Step([_Action("done", {"text": "All done, everything went great!"})],
              [ActionResult(is_done=True, extracted_content="All done, everything went great!")]),
    ])
    agent._orch = SimpleNamespace(agents={"main": failing}, session_id="s-err", usage_tracker=None)
    disp = GoalDispatcher(board, agent)
    goal = Goal(id="g-allerr", user_id="u1", title="do the thing")
    asyncio.run(disp._run_goal(goal))
    assert not board.successes, "done() on top of nothing but errors must not be a success"
    assert board.failures and "errored" in board.failures[0][1]


def test_success_notify_carries_honest_text(monkeypatch):
    pushed = []

    async def _push(goal, session_id, final, **kw):
        pushed.append(final)

    board = _FakeBoard()
    # §3.4: the fallback push only fires when the agent said NOTHING — use a
    # silent run so the notify path is exercised.
    agent = _CaseStudyAgent(done_text="Report written.\nOUTCOME: workspace/report.md",
                            with_send=False)
    disp = GoalDispatcher(board, agent)
    monkeypatch.setattr(disp, "_notify_owner_done", _push)
    monkeypatch.setenv("GOAL_NOTIFY_ON_DONE", "true")
    goal = Goal(id="g-honest", user_id="u1", title="write the report")
    asyncio.run(disp._run_goal(goal))
    assert pushed and "OUTCOME: workspace/report.md" in pushed[0]
    assert "Processing actions" not in pushed[0]


# ---------------------------------------------------------------------------
# 012 #1 — every failure-classified path must thread the run's REAL provenance
# (steps/spend_usd/artifacts) into finalize_episode, never the zero defaults.
# 015 #3 — a permanent LLM/provider failure writes a DISTINCT greppable marker
# (llm_provider_exhausted:) into goals.last_failure_error.
# ---------------------------------------------------------------------------

from agents.task.goals.dispatcher import LLM_EXHAUSTED_MARKER  # noqa: E402
from agents.task.runtime.run_outcome import RunOutcome  # noqa: E402
from core.exceptions import LLMPermanentError  # noqa: E402

_REAL_ARTIFACTS = [{"path": "workspace/report.md"}]


def _outcome(**kw) -> RunOutcome:
    """A run envelope that DID real work (steps=7, spend=$0.42, 1 artifact)."""
    base = dict(session_id="s-prov", status="Session completed successfully",
                steps=7, spend_usd=0.42, artifacts=list(_REAL_ARTIFACTS))
    base.update(kw)
    return RunOutcome(**base)


def _patch_run(monkeypatch, outcome=None, exc=None):
    """Replace the shared run helper so the test controls the RunOutcome/raise."""

    async def _fake(task_agent, *, user_id, request, autonomous=False):
        if exc is not None:
            raise exc
        return outcome

    monkeypatch.setattr("agents.task.goals.dispatcher._run_task_to_outcome", _fake)


def _capture_finalize(monkeypatch):
    calls = []

    async def _fin(**kw):
        calls.append(kw)

    monkeypatch.setattr("modules.memory.episodic.finalize_episode", _fin)
    return calls


def _dispatcher():
    board = _FakeBoard()
    agent = SimpleNamespace(deliver_self_wake=None, container=None)
    return board, GoalDispatcher(board, agent)


def _assert_real_provenance(call):
    assert call["outcome"] == "failed"
    assert call["steps"] == 7, "episodes must record the run's REAL step count"
    assert call["spend_usd"] == 0.42, "episodes must record the run's REAL spend"
    assert call["artifacts"] == _REAL_ARTIFACTS, "episodes must record the run's artifacts"


def test_blocked_declared_failure_threads_real_provenance(monkeypatch):
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(
        done_called=True, blocked=True, blocked_need="an x402 store"))
    calls = _capture_finalize(monkeypatch)
    asyncio.run(disp._run_goal(Goal(id="g-b", user_id="u1", title="t")))
    assert board.failures and not board.successes
    assert calls, "a blocked-declared failure must still finalize the episode"
    _assert_real_provenance(calls[0])


def test_no_done_failure_threads_real_provenance(monkeypatch):
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(done_called=False))
    calls = _capture_finalize(monkeypatch)
    asyncio.run(disp._run_goal(Goal(id="g-nd", user_id="u1", title="t")))
    assert board.failures and not board.successes
    assert calls
    _assert_real_provenance(calls[0])


def test_all_actions_errored_failure_threads_real_provenance(monkeypatch):
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(done_called=True, all_actions_errored=True))
    calls = _capture_finalize(monkeypatch)
    asyncio.run(disp._run_goal(Goal(id="g-ae", user_id="u1", title="t")))
    assert board.failures and not board.successes
    assert calls
    _assert_real_provenance(calls[0])


def test_refusal_failure_threads_envelope_provenance(monkeypatch):
    """The refusal path passes whatever the envelope holds (real values when work
    preceded the refusal) instead of silently taking finalize's zero defaults."""
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(
        refusal=True, status="No active session found",
        steps=3, spend_usd=0.05, artifacts=[]))
    calls = _capture_finalize(monkeypatch)
    asyncio.run(disp._run_goal(Goal(id="g-r", user_id="u1", title="t")))
    assert board.failures and not board.successes
    # a plain refusal keeps the legacy, non-marker error string
    assert board.failures[0][1] == "run did not complete (refusal or empty)"
    assert calls and calls[0]["steps"] == 3 and calls[0]["spend_usd"] == 0.05


class _ExplodingOutcome:
    """Envelope that did real work, then makes the dispatcher raise mid-
    classification — exercising the OUTER exception handler with `run` set."""

    session_id = "s-exp"
    refusal = False
    steps = 7
    spend_usd = 0.42
    artifacts = list(_REAL_ARTIFACTS)
    outcome_line = None

    def result_text(self):
        return "did work"

    @property
    def blocked(self):
        raise RuntimeError("kaboom mid-classification")


def test_exception_after_work_threads_real_provenance(monkeypatch):
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _ExplodingOutcome())
    calls = _capture_finalize(monkeypatch)
    asyncio.run(disp._run_goal(Goal(id="g-exc", user_id="u1", title="t")))
    assert board.failures and not board.successes
    # a generic crash must NOT be labeled as provider exhaustion
    assert not board.failures[0][1].startswith(LLM_EXHAUSTED_MARKER)
    assert calls
    _assert_real_provenance(calls[0])


# --- 015 #3: the distinct llm_provider_exhausted marker --------------------


def test_llm_permanent_error_exception_writes_marker(monkeypatch):
    board, disp = _dispatcher()
    _patch_run(monkeypatch, exc=LLMPermanentError(
        "OpenRouter generation failed: Error code: 402 - This request requires more credits"))
    asyncio.run(disp._run_goal(Goal(id="g-402", user_id="u1", title="t")))
    assert board.failures
    err = board.failures[0][1]
    assert err.startswith(LLM_EXHAUSTED_MARKER + ":"), err
    assert "402" in err


def test_llm_permanent_error_in_cause_chain_writes_marker(monkeypatch):
    exc = RuntimeError("goal run wrapper failed")
    exc.__cause__ = LLMPermanentError("account quota exceeded")
    board, disp = _dispatcher()
    _patch_run(monkeypatch, exc=exc)
    asyncio.run(disp._run_goal(Goal(id="g-chain", user_id="u1", title="t")))
    assert board.failures
    assert board.failures[0][1].startswith(LLM_EXHAUSTED_MARKER + ":")


def test_provider_death_refusal_status_writes_marker(monkeypatch):
    """The LIVE prod signature: run_session returns the halted-session refusal
    string — the marker must replace the generic 'did not complete' error."""
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(
        refusal=True, steps=0, spend_usd=0.0, artifacts=[],
        status=("Session failed: PERMANENT ERROR: OpenRouter generation failed: "
                "Error code: 402 - insufficient credits. Session halted.")))
    asyncio.run(disp._run_goal(Goal(id="g-rf402", user_id="u1", title="t")))
    assert board.failures
    err = board.failures[0][1]
    assert err.startswith(LLM_EXHAUSTED_MARKER + ":"), err
    assert "402" in err


def test_all_providers_exhausted_refusal_writes_marker(monkeypatch):
    """The exhausted-fallback halt string carries no 402/billing text — the
    phrasing itself must classify."""
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(
        refusal=True, steps=0, spend_usd=0.0, artifacts=[],
        status="Session failed: All LLM providers failed. Tried: ['openrouter']. Session halted."))
    asyncio.run(disp._run_goal(Goal(id="g-exh", user_id="u1", title="t")))
    assert board.failures
    assert board.failures[0][1].startswith(LLM_EXHAUSTED_MARKER + ":")


def test_generic_exception_keeps_plain_error(monkeypatch):
    board, disp = _dispatcher()
    _patch_run(monkeypatch, exc=RuntimeError("disk full while writing report"))
    asyncio.run(disp._run_goal(Goal(id="g-gen", user_id="u1", title="t")))
    assert board.failures
    assert not board.failures[0][1].startswith(LLM_EXHAUSTED_MARKER)


def test_planner_outage_logs_marker_and_skips_escalation(monkeypatch, caplog):
    """015 #3 planner leg: a planner run killed by provider exhaustion logs the
    distinct marker and does NOT count as an empty-pipeline stall."""
    import logging
    import agents.task.goals.dispatcher as disp
    import agents.task.goals.planner as planner
    import core.credit_sentinel as cs

    async def fake_run(task_agent, *, user_id, request, autonomous):
        return "sess-outage", None

    monkeypatch.setattr(disp, "_run_task_as_session", fake_run)
    monkeypatch.setattr(planner, "build_planner_prompt", lambda *a, **k: "P")
    monkeypatch.setattr(cs, "credit_sentinel_active", lambda: True)

    escalated = []

    async def esc(user_id, *, planner_summary=None):
        escalated.append(user_id)

    fake = SimpleNamespace(board=object(), task_agent=None,
                           _maybe_escalate_empty_pipeline=esc)
    with caplog.at_level(logging.ERROR, logger=disp.logger.name):
        asyncio.run(disp.GoalDispatcher._run_planner(fake, "rob"))
    assert any(disp.LLM_EXHAUSTED_MARKER in r.getMessage() for r in caplog.records)
    assert escalated == [], "outage must not be treated as an empty-pipeline stall"


def test_planner_normal_run_still_escalates(monkeypatch, caplog):
    import logging
    import agents.task.goals.dispatcher as disp
    import agents.task.goals.planner as planner
    import core.credit_sentinel as cs

    async def fake_run(task_agent, *, user_id, request, autonomous):
        return "sess-ok", "queued 2 goals"

    monkeypatch.setattr(disp, "_run_task_as_session", fake_run)
    monkeypatch.setattr(planner, "build_planner_prompt", lambda *a, **k: "P")
    monkeypatch.setattr(cs, "credit_sentinel_active", lambda: False)

    escalated = []

    async def esc(user_id, *, planner_summary=None):
        escalated.append((user_id, planner_summary))

    fake = SimpleNamespace(board=object(), task_agent=None,
                           _maybe_escalate_empty_pipeline=esc)
    with caplog.at_level(logging.INFO, logger=disp.logger.name):
        asyncio.run(disp.GoalDispatcher._run_planner(fake, "rob"))
    assert any("goal planner ran" in r.getMessage() for r in caplog.records)
    assert not any(disp.LLM_EXHAUSTED_MARKER in r.getMessage() for r in caplog.records)
    assert escalated == [("rob", "queued 2 goals")]
