"""Task 12: an errored, router-bound session must never be silent.

2026-07-16 incident: the owner asked "what did you spend money on?", OpenRouter
was out of credits, the LLM 402'd on step 1, the session failed 14 seconds
later -- and the owner got absolute silence. The C10 bound-session skip in
``_run_and_deliver`` assumed the router "already delivered live"
UNCONDITIONALLY -- false for an errored run, where the agent never reaches
send_message/done, so nothing was delivered live AND the fallback was
skipped.

``run_session`` returns a human-readable STRING, not a status enum (verified
in agents/task_agent_lite.py:961-976) -- the only correct failure predicate is
``status.startswith("Session failed:")``.
"""
import pytest

from surfaces.telegram import harness


@pytest.fixture(autouse=True)
def _reset_outage_cooldown():
    """015 #2: the LLM-outage notice keeps an in-process per-chat cooldown; both
    credit-death tests below use session 'sess-1' and would suppress each other
    without a reset."""
    from core.surfaces.llm_outage_notice import reset_llm_outage_notice_state
    reset_llm_outage_notice_state()
    yield
    reset_llm_outage_notice_state()


class _Orch:
    def __init__(self, bound=True):
        self._message_router = object() if bound else None
        self._chat_session_key = "chat:1" if bound else None


class _Agent:
    """run_session returns the real human-readable strings (see the table above)."""
    def __init__(self, status="Session completed successfully", reply=""):
        self.status, self.reply = status, reply
        self.orch = _Orch(bound=True)

    async def run_session(self, user_id, session_id):
        return self.status

    def get_orchestrator(self, session_id):
        return self.orch

    def _extract_chat_reply(self, session_id):
        return self.reply


async def _deliver_with(agent):
    sent = []

    async def deliver(m):
        sent.append(m)
    await harness._run_and_deliver(agent, "rob", "sess-1", deliver)
    return sent


@pytest.mark.asyncio
async def test_errored_bound_session_still_tells_the_owner():
    """2026-07-16: the session was router-bound, the LLM 402'd on step 1, so
    nothing was delivered live AND the C10 branch skipped the fallback ->
    total silence on an owner question."""
    sent = await _deliver_with(_Agent(
        status="Session failed: OpenRouter generation failed: Error code: 402", reply=""))
    assert sent, "an errored run must not be silent"
    assert "couldn't" in sent[0].lower()


@pytest.mark.asyncio
async def test_successful_bound_session_does_not_double_send():
    """The C10 guard must still hold for a run that DID deliver live."""
    sent = await _deliver_with(_Agent(
        status="Session completed successfully", reply="here is your answer"))
    assert sent == []


@pytest.mark.asyncio
async def test_busy_session_is_not_reported_as_a_failure():
    """"Session is already executing" is a busy no-op, not an error -- sending an
    error notice for it would spam the owner on every concurrent turn."""
    sent = await _deliver_with(_Agent(status="Session is already executing", reply=""))
    assert sent == []


@pytest.mark.asyncio
async def test_suspended_bound_session_still_tells_the_owner():
    """The OTHER credit-death path (final review Finding 3): TaskAgent.run_session
    returns "Session suspended: ...credits..." from ``except InsufficientCreditsError``
    (agents/task_agent_lite.py), NOT "Session failed: ...". The ``failed`` predicate
    only matched the "Session failed:" prefix, so a suspended run on a router-bound
    session hit the exact same silent-return path the "Session failed:" fix above
    closed -- verbatim the outage this module exists to prevent, on the credits path
    the owner actually hits."""
    sent = await _deliver_with(_Agent(
        status="Session suspended: Insufficient credits. Add credits to resume.",
        reply=""))
    assert sent, "a credit-suspended run must not be silent"
    assert "couldn't" in sent[0].lower()
