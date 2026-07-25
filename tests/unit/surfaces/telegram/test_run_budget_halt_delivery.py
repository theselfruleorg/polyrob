"""T1.1 enable-blocker (validated 2026-07-23): a RUN_BUDGET_USD halt on a
CONTINUED session ends the turn before any new step, so the post-run reply
extraction surfaces the PREVIOUS turn's answer as if it answered the new
message. The harness must deliver the honest halt text instead (mirror of
chat_once's marker branch in 9aebd663)."""
import asyncio

from surfaces.telegram import harness

BUDGET_HALT = (
    "Session failed: run_budget_exhausted: session provider spend $0.4020 "
    "reached RUN_BUDGET_USD $0.40; halting before the next step "
    "(raise the budget or start a new session to continue)"
)

STALE_REPLY = "Here is my answer to your PREVIOUS question."


class _Agent:
    def __init__(self, status, stale_reply=STALE_REPLY):
        self._status = status
        self._stale = stale_reply

    async def run_session(self, user_id, session_id):
        return self._status

    def get_orchestrator(self, session_id):
        return None

    def _extract_chat_reply(self, session_id):
        return self._stale


def _run(agent):
    delivered = []

    async def deliver(text):
        delivered.append(text)

    asyncio.run(harness._run_and_deliver(agent, "u1", "s1", deliver))
    return delivered


def test_budget_halt_delivers_halt_text_not_stale_reply():
    delivered = _run(_Agent(BUDGET_HALT))
    assert len(delivered) == 1
    assert "run_budget_exhausted" in delivered[0]
    assert STALE_REPLY not in delivered[0]


def test_non_budget_failure_keeps_the_legacy_extraction_rail():
    # Pre-existing behavior for other failure strings is deliberately unchanged
    # (chat_once's fix scoped itself the same way) — pin it so the marker branch
    # stays narrow.
    delivered = _run(_Agent("Session failed: boom"))
    assert delivered == [STALE_REPLY]
