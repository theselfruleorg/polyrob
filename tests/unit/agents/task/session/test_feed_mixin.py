"""FeedMixin.add_to_feed actually lands a feed entry.

Regression: FeedMixin.add_to_feed used to ``await self.session_manager.
add_to_feed(session_id, agent_id, entry_type, data)`` — 4 positional args into
the SYNCHRONOUS 3-param ``SessionManager.add_to_feed(session_id, event_type,
data)`` — so EVERY call raised TypeError, silently swallowed at debug level by
the two callers (memory_prefetch/memory_writer feed mirrors). The fix keeps
SessionManager.add_to_feed sync with the legacy 3-positional shape (api/tools
callers depend on it) and adds a trailing ``agent_id`` keyword.
"""
import json
import logging

import pytest

from agents.task.agent.session import SessionManager
from agents.task.session.feed import FeedMixin


class _Orchestrator(FeedMixin):
    """Minimal orchestrator stub carrying just what FeedMixin needs."""

    def __init__(self, session_manager, session_id):
        self.session_manager = session_manager
        self.session_id = session_id
        self.logger = logging.getLogger("test.feed")


@pytest.mark.asyncio
async def test_add_to_feed_lands_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from agents.task.path import reset_path_manager
    reset_path_manager()

    sm = SessionManager(base_dir=str(tmp_path))
    orch = _Orchestrator(sm, "sess-feed-1")

    # Must not raise (the old arity/await mismatch raised TypeError here).
    await orch.add_to_feed("agent-7", "memory_recall", {"count": 2})

    entries = list(tmp_path.rglob("memory_recall_*.json"))
    assert len(entries) == 1, f"expected one feed entry, found {entries}"
    payload = json.loads(entries[0].read_text())
    # Preserve the format other readers (webview/_process_feed_metadata) parse.
    assert payload["type"] == "memory_recall"
    assert payload["data"] == {"count": 2}
    assert "timestamp" in payload
    assert payload["agent_id"] == "agent-7"


def test_legacy_three_arg_call_still_valid(tmp_path, monkeypatch):
    """api/tools call session_manager.add_to_feed(session_id, event_type, data)."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from agents.task.path import reset_path_manager
    reset_path_manager()

    sm = SessionManager(base_dir=str(tmp_path))
    sm.add_to_feed("sess-feed-2", "agent_message", {"text": "hi"})

    entries = list(tmp_path.rglob("agent_message_*.json"))
    assert len(entries) == 1
    payload = json.loads(entries[0].read_text())
    assert payload["type"] == "agent_message"
    assert payload["data"] == {"text": "hi"}
    assert "agent_id" not in payload  # legacy shape unchanged
