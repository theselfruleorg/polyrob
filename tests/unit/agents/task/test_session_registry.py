"""Unit tests for SessionRegistry and TaskAgent's orchestrator accessors (PR7).

These pin the de-duplication seam: the active-orchestrator store is reached
only through the registry interface / public accessors, and the legacy
``_active_orchestrators`` attribute remains a compatibility view over it.
"""

import pytest

from agents.task.session_registry import SessionRegistry
from agents.task_agent_lite import TaskAgent


class _Orch:
    """Stand-in orchestrator with an identifiable marker."""

    def __init__(self, marker):
        self.marker = marker


# --- SessionRegistry ---


def test_register_and_get():
    reg = SessionRegistry()
    o = _Orch("a")
    reg.register("s1", o)
    assert reg.get("s1") is o
    assert reg.get("missing") is None


def test_register_replaces():
    reg = SessionRegistry()
    reg.register("s1", _Orch("old"))
    new = _Orch("new")
    reg.register("s1", new)
    assert reg.get("s1") is new
    assert reg.count() == 1


def test_remove_returns_and_pops():
    reg = SessionRegistry()
    o = _Orch("a")
    reg.register("s1", o)
    assert reg.remove("s1") is o
    assert reg.get("s1") is None
    # removing an absent key is a no-op returning None
    assert reg.remove("s1") is None


def test_contains_count_len_iter():
    reg = SessionRegistry()
    reg.register("s1", _Orch("a"))
    reg.register("s2", _Orch("b"))
    assert reg.contains("s1")
    assert "s2" in reg
    assert "nope" not in reg
    assert reg.count() == 2
    assert len(reg) == 2
    assert set(iter(reg)) == {"s1", "s2"}


def test_items_and_values_are_snapshots():
    reg = SessionRegistry()
    a, b = _Orch("a"), _Orch("b")
    reg.register("s1", a)
    reg.register("s2", b)

    items = reg.items()
    values = reg.values()
    # snapshots: mutating the registry mid-iteration must not raise
    for sid, _ in items:
        reg.remove(sid)
    assert reg.count() == 0
    assert set(values) == {a, b}


def test_clear():
    reg = SessionRegistry()
    reg.register("s1", _Orch("a"))
    reg.clear()
    assert reg.count() == 0


# --- TaskAgent accessors + compat property ---
#
# Build a bare TaskAgent (no container/config) and attach a fresh registry,
# exercising only the accessor surface — no I/O, no initialization.


def _bare_agent():
    agent = object.__new__(TaskAgent)
    agent._registry = SessionRegistry()
    return agent


def test_taskagent_register_get_remove():
    agent = _bare_agent()
    o = _Orch("x")
    agent.register_orchestrator("s1", o)
    assert agent.get_orchestrator("s1") is o
    assert agent.active_session_count() == 1
    assert agent.active_orchestrators() == [o]
    assert agent.remove_orchestrator("s1") is o
    assert agent.get_orchestrator("s1") is None


def test_compat_property_reads_registry():
    agent = _bare_agent()
    o = _Orch("x")
    agent.register_orchestrator("s1", o)
    # legacy attribute reflects the registry contents
    assert agent._active_orchestrators["s1"] is o
    assert len(agent._active_orchestrators) == 1


def test_compat_property_mutation_visible_to_registry():
    agent = _bare_agent()
    o = _Orch("x")
    # legacy direct mutation still lands in the registry
    agent._active_orchestrators["s1"] = o
    assert agent.get_orchestrator("s1") is o


def test_compat_property_setter_replaces_store():
    agent = _bare_agent()
    agent.register_orchestrator("old", _Orch("old"))
    agent._active_orchestrators = {}  # tests reset the registry this way
    assert agent.active_session_count() == 0
    assert agent.get_orchestrator("old") is None
