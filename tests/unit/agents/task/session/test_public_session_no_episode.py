"""044 C5: a PUBLIC (room) session writes NO episode into the owner's store.

`cleanup()` gated the episodic write on `is_autonomous` alone, so a room session
— which runs AS the owner tenant, whoever opened it — closed by writing a row
into the owner's episodic history. That store is read back by the session-start
digest, the continuity bridge and `/recap`, all of which run in the owner's DM:
a stranger's room conversation became something the agent recalled to the owner
later. It also made "no memory in a room" only half true.
"""
import logging
import types

import pytest

from agents.task.session import cleanup as cleanup_mod


class _Orch(cleanup_mod.SessionCleanupMixin):
    """The least state `cleanup(full_cleanup=True)` needs to reach the write."""

    def __init__(self, *, public: bool):
        self.session_id = "room-sess-1"
        self.user_id = "owner1"
        self._public_session = public
        self.agents = {}
        self.logger = logging.getLogger("test.public_no_episode")
        self.container = None
        self.session_manager = None
        self.browser_manager = None
        self.controller = None
        self._chat_session_key = "agent:main:telegram:supergroup:-100"


@pytest.fixture
def captured(monkeypatch):
    calls = []

    async def _finalize(**kwargs):
        calls.append(kwargs)

    async def _provenance(_orch):
        return {"spend_usd": 0.0, "steps": 0, "artifacts": []}

    import modules.memory.episodic as episodic
    monkeypatch.setattr(episodic, "finalize_episode", _finalize)
    monkeypatch.setattr(episodic, "collect_provenance", _provenance)
    monkeypatch.setattr("agents.task.goals.autonomy_marker.is_autonomous",
                        lambda _sid: False)
    return calls


@pytest.mark.asyncio
async def test_public_session_writes_no_episode(captured):
    await _Orch(public=True).cleanup(status="completed", full_cleanup=True)
    assert captured == [], "a room session wrote into the owner's episodic store"


@pytest.mark.asyncio
async def test_private_session_still_writes_its_episode(captured):
    """The control: the guard must not have killed the chat episode outright."""
    await _Orch(public=False).cleanup(status="completed", full_cleanup=True)
    assert len(captured) == 1
    assert captured[0]["kind"] == "chat" and captured[0]["outcome"] == "done"
