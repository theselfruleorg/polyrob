"""Cross-session recall must use a distinct origin from in-session MEMORY.

Today both H-MEM (foundation) and prefetch (ephemeral) emit MessageOrigin.MEMORY,
which renders the same <session-memory> envelope — two indistinguishable blocks.
Cross-session prefetch must use MessageOrigin.RECALL + its own envelope tag.
"""
import inspect
from modules.llm.messages import MessageOrigin, _ORIGIN_ENVELOPE
from agents.task.agent.core import memory_prefetch


def test_recall_origin_exists():
    assert hasattr(MessageOrigin, "RECALL"), "cross-session recall needs its own origin"
    assert _ORIGIN_ENVELOPE.get(MessageOrigin.RECALL), "RECALL needs an envelope tag"


def test_prefetch_uses_recall_not_memory():
    src = inspect.getsource(memory_prefetch)
    assert "MessageOrigin.RECALL" in src, "prefetch must tag its block MessageOrigin.RECALL"
    assert "MessageOrigin.MEMORY" not in src, "prefetch must not emit a second MEMORY block"
