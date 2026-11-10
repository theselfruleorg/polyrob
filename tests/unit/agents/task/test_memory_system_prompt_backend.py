"""<memory-system> follows the configured backend and the session's registry.

Coherency review 2026-10-13 E-A11: no cross-session recall claim when
MEMORY_BACKEND=none registers no provider. 095 W9: nothing renders then (no
section, no identity clause); with a backend, one line per REGISTERED store.
"""

import pytest

from agents.task.agent.prompt_memory import memory_system_content
from agents.task.agent.prompts import SystemPrompt

ALL = {"session_search", "load_skill", "owner_doc_manage", "recent_activity"}


def _text():
    return SystemPrompt.__new__(SystemPrompt)._get_memory_system_content()


@pytest.mark.parametrize("value", ["none", "off", ""])
def test_no_backend_renders_nothing(monkeypatch, value):
    monkeypatch.setenv("MEMORY_BACKEND", value)
    assert _text() == ""
    prompt = SystemPrompt("- session_search: x", use_native_tools=True,
                          tool_ids=[]).get_system_message().content
    assert "<memory-system>" not in prompt
    assert "cross-session memory" not in prompt


@pytest.mark.parametrize("value", ["sqlite", "local_vector", "SQLite "])
def test_backend_on_teaches_each_registered_store(monkeypatch, value):
    monkeypatch.setenv("MEMORY_BACKEND", value)
    text = memory_system_content(ALL)
    for needle in ("`session_search`", "Recalled from memory", "`memory` field",
                   "`load_skill`", "owner.md", "`recent_activity`"):
        assert needle in text, needle
    assert len(text.splitlines()) <= 7
    prompt = SystemPrompt("- session_search: x", use_native_tools=True,
                          tool_ids=[]).get_system_message().content
    assert "<memory-system>" in prompt
    assert "specialist with cross-session memory" in prompt


def test_an_unregistered_store_is_not_named(monkeypatch):
    monkeypatch.setenv("MEMORY_BACKEND", "sqlite")
    text = memory_system_content({"load_skill"})
    assert "session_search" not in text
    assert "recent_activity" not in text
    assert "owner_doc_manage" not in text
    assert "`load_skill`" in text


def test_collection_shape_only_when_the_kb_is_on(monkeypatch):
    import core.config_policy as cp
    monkeypatch.setenv("MEMORY_BACKEND", "sqlite")
    monkeypatch.setattr(cp.AutonomyConfig, "kb_enabled", staticmethod(lambda: False))
    assert "collection=" not in memory_system_content({"session_search"})
    monkeypatch.setattr(cp.AutonomyConfig, "kb_enabled", staticmethod(lambda: True))
    assert "collection=" in memory_system_content({"session_search"})


def test_the_prompt_does_not_say_the_memory_field_is_recalled():
    """Only reflection summaries reach the store: most steps never do."""
    from agents.task.agent import prompt_memory
    assert "is what later sessions recall" not in prompt_memory._BRAIN
    assert "note for this run" in prompt_memory._BRAIN


def test_the_unused_brain_fields_are_gone():
    from agents.task.agent.views import AgentBrain
    fields = set(AgentBrain.model_fields)
    assert not fields & {"working_memory", "confidence_level", "acceptance_criteria"}
    # A model that still sends one is read, not refused.
    brain = AgentBrain(evaluation_previous_goal="e", memory="m", next_goal="n", confidence_level=0.5)
    assert brain.memory == "m"
