"""Tests for agents.task.runtime.run_as_session."""
import pytest
from agents.task.runtime.run_as_session import is_refusal, run_task_as_session


def test_is_refusal_matches_known_refusals():
    assert is_refusal("No active session found") is True
    assert is_refusal(None) is True
    assert is_refusal("Here is your summary.") is False


def test_is_refusal_known_prefix_variants():
    assert is_refusal("task package not available (reason)") is True
    assert is_refusal("session not found or unauthorized") is True
    assert is_refusal("session is already executing") is True
    assert is_refusal("") is True  # falsy → refusal


def test_is_refusal_genuine_result():
    assert is_refusal("Done! I scraped 42 pages.") is False
    assert is_refusal("The analysis is complete.") is False


# ---------------------------------------------------------------------------
# run_task_as_session — unit tests with a fake task_agent
# ---------------------------------------------------------------------------

class _FakeAgent:
    """Minimal stand-in for TaskAgentLite.

    ``final`` is what ``run_session`` returns (a STATUS string for the real agent).
    ``reply`` is what ``_extract_chat_reply`` returns (the agent's real output); when
    None, the extractor yields nothing so delivery falls back to the run_session return.
    """

    def __init__(self, session_id, final, reply=None):
        self._session_id = session_id
        self._final = final
        self._reply = reply

    async def create_session(self, *, user_id, request):
        if self._session_id is None:
            return {}
        return {"id": self._session_id}

    async def run_session(self, user_id, session_id):
        return self._final

    def _extract_chat_reply(self, session_id):
        return self._reply


@pytest.mark.asyncio
async def test_run_task_as_session_genuine_result():
    agent = _FakeAgent("sess-1", "Done! Task complete.")
    sid, final = await run_task_as_session(agent, user_id="u1", request={"task": "do X"})
    assert sid == "sess-1"
    assert final == "Done! Task complete."


@pytest.mark.asyncio
async def test_run_task_as_session_refusal_returns_none_final():
    agent = _FakeAgent("sess-2", "No active session found")
    sid, final = await run_task_as_session(agent, user_id="u1", request={"task": "do X"})
    assert sid == "sess-2"
    assert final is None  # refusal → second element is None


@pytest.mark.asyncio
async def test_run_task_as_session_no_session_id():
    agent = _FakeAgent(None, "irrelevant")
    sid, final = await run_task_as_session(agent, user_id="u1", request={"task": "do X"})
    assert sid is None
    assert final is None


# --- THE "blind digest" bug: deliver the agent's REAL output, not run_session's
#     status string ("Session completed successfully"). Same root as proposal 004. ---

@pytest.mark.asyncio
async def test_delivers_extracted_reply_not_status_string():
    """run_session returns a STATUS string; the delivered `final` must be the agent's
    real reply (via _extract_chat_reply), so cron digests / goal delivery report real
    content instead of the literal 'Session completed successfully'."""
    agent = _FakeAgent("s1", "Session completed successfully", reply="Here's my digest: I posted 3 tweets.")
    sid, final = await run_task_as_session(agent, user_id="u1", request={"task": "digest"})
    assert sid == "s1"
    assert final == "Here's my digest: I posted 3 tweets."
    assert final != "Session completed successfully"


@pytest.mark.asyncio
async def test_silent_marker_survives_so_delivery_can_suppress():
    """If the agent replied [SILENT], that must be what `final` carries so the delivery
    layer's is_silent() can suppress an empty run (impossible when final was the status
    string, which never contains [SILENT])."""
    agent = _FakeAgent("s2", "Session completed successfully", reply="[SILENT]")
    sid, final = await run_task_as_session(agent, user_id="u1", request={"task": "watch"})
    assert final == "[SILENT]"


@pytest.mark.asyncio
async def test_falls_back_to_run_return_when_extract_empty():
    """If extraction yields nothing (edge case), degrade to the run_session return —
    never worse than the pre-fix behavior."""
    agent = _FakeAgent("s3", "Session completed successfully", reply="")
    sid, final = await run_task_as_session(agent, user_id="u1", request={"task": "x"})
    assert final == "Session completed successfully"


@pytest.mark.asyncio
async def test_refusal_short_circuits_before_extraction():
    """A failed/refused run stays a refusal (final None) and must NOT deliver a stale
    extracted reply."""
    agent = _FakeAgent("s4", "Session failed: boom", reply="stale previous answer")
    sid, final = await run_task_as_session(agent, user_id="u1", request={"task": "x"})
    assert sid == "s4"
    assert final is None


# --- F7 (live-test): failed/suspended session returns must be refusals --------

def test_session_failed_is_refusal():
    from agents.task.runtime.run_as_session import is_refusal
    assert is_refusal("Session failed: PERMANENT ERROR: 402 ...") is True


def test_session_suspended_is_refusal():
    from agents.task.runtime.run_as_session import is_refusal
    assert is_refusal("Session suspended: insufficient credits. Add credits to resume.") is True


def test_genuine_completion_is_not_refusal():
    from agents.task.runtime.run_as_session import is_refusal
    # a real success must still be recorded as success
    assert is_refusal("Session completed successfully") is False
    assert is_refusal("Done — wrote report.md") is False


# --- T2-01: completed_via_done — distinguish a genuine done() from a stopped run ---

from agents.task.runtime.run_as_session import completed_via_done


class _Res:
    def __init__(self, is_done):
        self.is_done = is_done


class _AgentStub:
    def __init__(self, last_result, is_sub=False):
        self._last_result = last_result
        self._is_sub_agent = is_sub


class _Orch:
    def __init__(self, agents):
        # agents: dict id -> agent stub
        self.agents = agents


def test_completed_via_done_true_when_last_result_is_done():
    orch = _Orch({"main": _AgentStub([_Res(False), _Res(True)])})
    assert completed_via_done(orch) is True


def test_completed_via_done_false_when_ran_but_no_done():
    # last result exists (the loop ran) but nothing is_done -> exhausted / drifted
    orch = _Orch({"main": _AgentStub([_Res(False), _Res(False)])})
    assert completed_via_done(orch) is False


def test_completed_via_done_none_when_undeterminable():
    assert completed_via_done(None) is None                    # no orchestrator
    assert completed_via_done(_Orch({})) is None               # no agents
    assert completed_via_done(_Orch({"m": _AgentStub(None)})) is None  # no last_result read


def test_completed_via_done_ignores_sub_agents():
    # a done sub-agent must NOT count as the main run completing
    orch = _Orch({
        "sub": _AgentStub([_Res(True)], is_sub=True),
        "main": _AgentStub([_Res(False)]),
    })
    assert completed_via_done(orch) is False


def test_completed_via_done_true_if_any_main_agent_done():
    orch = _Orch({
        "a": _AgentStub([_Res(False)]),
        "b": _AgentStub([_Res(True)]),
    })
    assert completed_via_done(orch) is True
