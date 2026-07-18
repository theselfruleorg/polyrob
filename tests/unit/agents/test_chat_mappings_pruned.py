"""Regression (P7/F2 finalization): TaskAgent._chat_sessions / _chat_locks (chat-key ->
session_id and its lock) were pruned nowhere on eviction — they grew unbounded, one
stale entry per (user, chat) forever. Eviction must drop the entries for the evicted
session.
"""
import asyncio
import types

from agents.task_agent_lite import TaskAgent


def _agent():
    a = TaskAgent.__new__(TaskAgent)
    a._chat_sessions = {}
    a._chat_locks = {}
    return a


def test_prune_removes_only_the_evicted_sessions_entries():
    a = _agent()
    a._chat_sessions = {
        "chat:u1:c1": "sessA",
        "chat:u1:c2": "sessB",
        "chat:u2:c1": "sessA",  # a different chat mapped to the same session
    }
    a._chat_locks = {k: asyncio.Lock() for k in a._chat_sessions}

    a._prune_chat_mappings("sessA")

    assert a._chat_sessions == {"chat:u1:c2": "sessB"}
    assert set(a._chat_locks) == {"chat:u1:c2"}


def test_prune_is_safe_when_empty_or_absent():
    a = TaskAgent.__new__(TaskAgent)  # no _chat_sessions attr at all
    a._prune_chat_mappings("whatever")  # must not raise
    a2 = _agent()
    a2._prune_chat_mappings("nope")  # no matching entries — no-op
    assert a2._chat_sessions == {}
