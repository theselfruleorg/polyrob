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


def test_run_and_deliver_holds_the_workspace_for_the_turn(monkeypatch, tmp_path):
    """056 WS3: the owner's chat turn marks the process busy (so cron/goal ticks
    skip) and leaves a turn.active marker while run_session runs — and clears
    both afterwards."""
    from core import interactive_gate as g
    monkeypatch.setenv("POLYROB_WORKSPACE_LOCK_DIR", str(tmp_path / "locks"))
    g._busy_depth = 0
    seen = {}

    class _A(_Agent):
        async def run_session(self, user_id, session_id):
            seen["busy"] = g.is_interactive_busy()
            seen["marker"] = g.read_turn_marker()
            return "Session completed successfully"

    async def deliver(text):
        pass

    asyncio.run(harness._run_and_deliver(_A(bound=True), "u1", "s1", deliver))
    assert seen["busy"] is True
    assert seen["marker"] and seen["marker"]["kind"] == "owner_chat" and seen["marker"]["session_id"] == "s1"
    assert not g.is_interactive_busy() and g.read_turn_marker() is None
