"""C10: with SINGULAR_CHAT_ENABLED (default on for `polyrob telegram`), the send_message
(and now done) mirror delivers the reply LIVE through the MessageRouter, but
_run_and_deliver ALSO re-extracted the same AIMessage from history and sent it again —
so every simple chat turn was delivered twice (once MarkdownV2, once raw). When a chat
surface is bound, the harness must NOT post-run-deliver; unbound/legacy paths still do.
"""
import asyncio

from surfaces.telegram import harness


class _Orch:
    def __init__(self, bound):
        self._message_router = object() if bound else None
        self._chat_session_key = "chat:key" if bound else None


class _Agent:
    def __init__(self, bound, reply="hello"):
        self._orch = _Orch(bound)
        self._reply = reply

    async def run_session(self, user_id, session_id):
        return "Session completed successfully"

    def get_orchestrator(self, session_id):
        return self._orch

    def _extract_chat_reply(self, session_id):
        return self._reply


def test_bound_session_does_not_double_deliver():
    delivered = []

    async def deliver(text):
        delivered.append(text)

    agent = _Agent(bound=True)
    asyncio.run(harness._run_and_deliver(agent, "u1", "s1", deliver))
    assert delivered == []  # already delivered live via the router mirror


def test_unbound_session_still_delivers():
    delivered = []

    async def deliver(text):
        delivered.append(text)

    agent = _Agent(bound=False)
    asyncio.run(harness._run_and_deliver(agent, "u1", "s1", deliver))
    assert delivered == ["hello"]  # legacy path: harness delivers
