"""§2 RunOutcome — one canonical outcome object assembled at run end.

Proposal: docs/proposals/2026-07-09-intelligence-stack-finalization.md §2.
The live corruption (goal 58a1385d18bf): the agent exited honestly via
``done("OUTCOME: BLOCKED — …")`` but every consumer re-extracted strings from
message history and got the P2-16 placeholder "Processing actions". RunOutcome
sources ``done_text`` from the ACTION LEDGER (ActionResult.extracted_content),
never from a message-history AIMessage, so placeholder/status strings become
unrepresentable as results.
"""
import asyncio
from types import SimpleNamespace

import pytest

from tools.controller.types import ActionResult


# ---------------------------------------------------------------------------
# Fakes mirroring the real orchestrator/agent/history shapes
# ---------------------------------------------------------------------------

class _Action:
    """Stand-in for a registry ActionModel: model_dump() -> {name: params}."""

    def __init__(self, name, params=None):
        self._d = {name: params or {}}

    def model_dump(self, exclude_unset=True):
        return dict(self._d)


class _Step:
    def __init__(self, actions, results):
        self.model_output = SimpleNamespace(action=list(actions))
        self.result = list(results)


class _Agent:
    def __init__(self, steps, *, is_sub=False, n_steps=1):
        self.history = SimpleNamespace(history=list(steps))
        self._is_sub_agent = is_sub
        self._last_result = steps[-1].result if steps else None
        self.state = SimpleNamespace(n_steps=n_steps)


class _Orch:
    def __init__(self, agents, *, session_id="sess-1", usage_tracker=None):
        self.agents = agents
        self.session_id = session_id
        self.usage_tracker = usage_tracker


class _FakeTaskAgent:
    """Minimal TaskAgentLite stand-in with a resident orchestrator."""

    def __init__(self, orch=None, reply=""):
        self._orch = orch
        self._reply = reply

    def get_orchestrator(self, session_id):
        return self._orch

    def _extract_chat_reply(self, session_id):
        return self._reply


def _done_step(text):
    return _Step(
        [_Action("done", {"text": text})],
        [ActionResult(is_done=True, extracted_content=text)],
    )


LIVE_BLOCKED_TEXT = (
    "OUTCOME: BLOCKED — x402 payment request store unavailable (no database service)"
)


# ---------------------------------------------------------------------------
# extract_done_text — done() text comes from the action ledger
# ---------------------------------------------------------------------------

def test_done_text_extracted_from_action_ledger():
    from agents.task.runtime.run_outcome import extract_done_text
    orch = _Orch({"main": _Agent([
        _Step([_Action("filesystem_write_file")], [ActionResult(extracted_content="wrote a.md")]),
        _done_step(LIVE_BLOCKED_TEXT),
    ])})
    assert extract_done_text(orch) == LIVE_BLOCKED_TEXT


def test_done_text_reverse_scan_is_position_robust():
    """Unlike history.final_result() (reads history[-1].result[-1]), the ledger
    scan must find a done() that is not the very last result of the very last step."""
    from agents.task.runtime.run_outcome import extract_done_text
    done_res = ActionResult(is_done=True, extracted_content="OUTCOME: wrote report.md")
    trailing = ActionResult(extracted_content="post-done bookkeeping")
    orch = _Orch({"main": _Agent([
        _Step([_Action("done"), _Action("noop")], [done_res, trailing]),
    ])})
    assert extract_done_text(orch) == "OUTCOME: wrote report.md"


def test_done_text_skips_framework_strings():
    """A blocking send_message ends the turn with is_done=True but a framework
    string as extracted_content — that is not agent output."""
    from agents.task.runtime.run_outcome import extract_done_text
    orch = _Orch({"main": _Agent([
        _Step(
            [_Action("send_message", {"text": "here is my question"})],
            [ActionResult(
                is_done=True,
                extracted_content="Message sent to user. Task paused - will resume when user responds.",
            )],
        ),
    ])})
    assert extract_done_text(orch) == ""


def test_done_text_ignores_sub_agents():
    from agents.task.runtime.run_outcome import extract_done_text
    orch = _Orch({
        "sub": _Agent([_done_step("sub-agent done text")], is_sub=True),
        "main": _Agent([_Step([_Action("noop")], [ActionResult(extracted_content="x")])]),
    })
    assert extract_done_text(orch) == ""


def test_done_text_empty_when_no_orchestrator():
    from agents.task.runtime.run_outcome import extract_done_text
    assert extract_done_text(None) == ""


# ---------------------------------------------------------------------------
# collect_user_messages — the agent's send_message texts, from the ledger
# ---------------------------------------------------------------------------

def test_collect_user_messages_from_ledger():
    from agents.task.runtime.run_outcome import collect_user_messages
    orch = _Orch({"main": _Agent([
        _Step([_Action("send_message", {"text": "starting the task"})],
              [ActionResult(extracted_content="Message sent to user (non-blocking)")]),
        _Step([_Action("filesystem_write_file")], [ActionResult(extracted_content="ok")]),
        _Step([_Action("send_message", {"text": "hit a blocker: x402 store unavailable"})],
              [ActionResult(extracted_content="Message sent to user (non-blocking)")]),
    ])})
    assert collect_user_messages(orch) == [
        "starting the task",
        "hit a blocker: x402 store unavailable",
    ]


def test_collect_user_messages_skips_errors_and_sub_agents():
    from agents.task.runtime.run_outcome import collect_user_messages
    orch = _Orch({
        "sub": _Agent([_Step([_Action("send_message", {"text": "sub text"})],
                             [ActionResult(extracted_content="Message: sub text")])], is_sub=True),
        "main": _Agent([
            _Step([_Action("send_message", {"text": "failed send"})],
                  [ActionResult(error="boom")]),
            _Step([_Action("send_message", {"text": "good send"})],
                  [ActionResult(extracted_content="Message sent to user (non-blocking)")]),
        ]),
    })
    assert collect_user_messages(orch) == ["good send"]


# ---------------------------------------------------------------------------
# RunOutcome.result_text — placeholder/status strings unrepresentable
# ---------------------------------------------------------------------------

def test_result_text_prefers_done_text_over_reply():
    from agents.task.runtime.run_outcome import RunOutcome
    o = RunOutcome(session_id="s1", status="Session completed successfully",
                   done_text="OUTCOME: wrote report.md", reply_text="Processing actions")
    assert o.result_text() == "OUTCOME: wrote report.md"


def test_result_text_never_returns_placeholder_or_generic_status():
    from agents.task.runtime.run_outcome import RunOutcome
    o = RunOutcome(session_id="s1", status="Session completed successfully",
                   done_text="", reply_text="Processing actions")
    assert o.result_text() == ""


def test_result_text_falls_back_to_nongeneric_status():
    """Custom task_agents (and test fakes) return the real output as the
    run_session return value — a NON-generic status may surface."""
    from agents.task.runtime.run_outcome import RunOutcome
    o = RunOutcome(session_id="s1", status="report written to a.md",
                   done_text="", reply_text="")
    assert o.result_text() == "report written to a.md"


def test_result_text_empty_on_refusal():
    from agents.task.runtime.run_outcome import RunOutcome
    o = RunOutcome(session_id="s1", status="Session failed: boom", refusal=True,
                   done_text="", reply_text="stale previous answer")
    assert o.result_text() == ""


# ---------------------------------------------------------------------------
# build_run_outcome — full assembly
# ---------------------------------------------------------------------------

def test_build_run_outcome_live_case_replay():
    """THE case study (goal 58a1385d18bf): honest done('OUTCOME: BLOCKED — …'),
    poisoned reply extraction ('Processing actions'). The envelope must carry the
    honest signal: blocked=True, result_text is the done text, never the placeholder."""
    from agents.task.runtime.run_outcome import build_run_outcome
    orch = _Orch({"main": _Agent([
        _Step([_Action("send_message", {"text": "blocker report: x402 store unavailable"})],
              [ActionResult(extracted_content="Message sent to user (non-blocking)")]),
        _done_step(LIVE_BLOCKED_TEXT),
    ])})
    ta = _FakeTaskAgent(orch, reply="Processing actions")
    o = asyncio.run(build_run_outcome(ta, "s43de", "Session completed successfully"))
    assert o.done_called is True
    assert o.done_text == LIVE_BLOCKED_TEXT
    assert o.blocked is True
    assert "x402" in (o.blocked_need or "")
    assert o.result_text() == LIVE_BLOCKED_TEXT
    assert "Processing actions" not in o.result_text()
    assert o.user_messages == ["blocker report: x402 store unavailable"]


def test_build_run_outcome_refusal_short_circuits():
    from agents.task.runtime.run_outcome import build_run_outcome
    ta = _FakeTaskAgent(None, reply="stale")
    o = asyncio.run(build_run_outcome(ta, "s1", "Session failed: boom"))
    assert o.refusal is True
    assert o.result_text() == ""


def test_build_run_outcome_outcome_line_falls_back_to_reply():
    """Legacy compat: agents that put the OUTCOME line in a reply (not done())
    keep their BLOCKED declarations honored."""
    from agents.task.runtime.run_outcome import build_run_outcome
    orch = _Orch({"main": _Agent([_Step([_Action("noop")], [ActionResult(extracted_content="x")])])})
    ta = _FakeTaskAgent(orch, reply="cannot proceed\nOUTCOME: BLOCKED — need credentials")
    o = asyncio.run(build_run_outcome(ta, "s1", "Session completed successfully"))
    assert o.blocked is True
    assert o.blocked_need == "need credentials"


def test_build_run_outcome_outcome_line_falls_back_to_status():
    """Fake/custom agents return the OUTCOME line as the run_session status."""
    from agents.task.runtime.run_outcome import build_run_outcome
    ta = _FakeTaskAgent(None, reply="")
    o = asyncio.run(build_run_outcome(
        ta, "s1", "Drafted the tweet but cannot post.\nOUTCOME: BLOCKED — Twitter write is disabled"))
    assert o.blocked is True
    assert o.blocked_need == "Twitter write is disabled"


def test_build_run_outcome_provenance():
    from agents.task.runtime.run_outcome import build_run_outcome

    class _Tracker:
        async def get_session_breakdown(self, session_id):
            return {"total_user_cost_usd": 0.42}

    orch = _Orch({"main": _Agent([_done_step("OUTCOME: NONE — nothing to do")], n_steps=7)},
                 usage_tracker=_Tracker())
    ta = _FakeTaskAgent(orch, reply="")
    o = asyncio.run(build_run_outcome(ta, "sess-1", "Session completed successfully"))
    assert o.steps == 7
    assert o.spend_usd == pytest.approx(0.42)


def test_build_run_outcome_fail_open_without_orchestrator_accessor():
    """Task-agent fakes without get_orchestrator/_extract_chat_reply must not crash."""
    from agents.task.runtime.run_outcome import build_run_outcome

    class _Bare:
        pass

    o = asyncio.run(build_run_outcome(_Bare(), "s1", "custom final text"))
    assert o.done_called is None
    assert o.result_text() == "custom final text"


# ---------------------------------------------------------------------------
# run_task_to_outcome — the new primary entry in run_as_session
# ---------------------------------------------------------------------------

class _RunFake:
    def __init__(self, session_id, status, reply=""):
        self._session_id = session_id
        self._status = status
        self._reply = reply

    async def create_session(self, *, user_id, request):
        return {"id": self._session_id} if self._session_id else {}

    async def run_session(self, user_id, session_id):
        return self._status

    def _extract_chat_reply(self, session_id):
        return self._reply


@pytest.mark.asyncio
async def test_run_task_to_outcome_no_session():
    from agents.task.runtime.run_as_session import run_task_to_outcome
    o = await run_task_to_outcome(_RunFake(None, "x"), user_id="u1", request={"task": "t"})
    assert o.session_id is None


@pytest.mark.asyncio
async def test_run_task_to_outcome_refusal():
    from agents.task.runtime.run_as_session import run_task_to_outcome
    o = await run_task_to_outcome(
        _RunFake("s1", "Session failed: boom", reply="stale"), user_id="u1", request={"task": "t"})
    assert o.session_id == "s1"
    assert o.refusal is True
    assert o.result_text() == ""


@pytest.mark.asyncio
async def test_run_task_to_outcome_success_carries_reply():
    from agents.task.runtime.run_as_session import run_task_to_outcome
    o = await run_task_to_outcome(
        _RunFake("s1", "Session completed successfully", reply="Here is the digest."),
        user_id="u1", request={"task": "t"})
    assert o.session_id == "s1"
    assert o.refusal is False
    assert o.result_text() == "Here is the digest."


@pytest.mark.asyncio
async def test_run_task_to_outcome_marks_autonomous():
    from agents.task.runtime.run_as_session import run_task_to_outcome
    from agents.task.goals.autonomy_marker import is_autonomous
    await run_task_to_outcome(
        _RunFake("sess-auto", "Session completed successfully"),
        user_id="u1", request={"task": "t"}, autonomous=True)
    assert is_autonomous("sess-auto") is True
