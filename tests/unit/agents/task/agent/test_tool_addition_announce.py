"""F9 shape (b) — the agent SURFACES a late tool with one `<tool-addition>` (2026-09-23).

In `deferred` mode a late action is emitted carrying `defer_loading`, so the
provider knows it but the model's context does not hold it. It becomes callable
only when a `tool_addition` message names it. `refresh_tool_catalog` runs every
step before assembly, so that is where the announcement is made: ONE durable
control message, body = exactly one action name per line (the Anthropic wire
layer parses it back — `modules/llm/deferred_tools.py`).

Nothing is pushed in `grow` (the tools simply appeared) or in `bridge` (the
model reaches them through `tool_call`), and nothing is pushed on a model that
cannot accept mid-conversation tool changes.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.core.runtime_catalog import refresh_tool_catalog
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import MessageOrigin


def _mm(model="claude-opus-5") -> MessageManager:
    llm = MagicMock()
    llm.model_name = model
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=4000,
    )


def _agent(mm, late_names=(), provider="anthropic"):
    registry = SimpleNamespace(late_action_names=lambda: tuple(late_names))
    controller = SimpleNamespace(registry=registry)
    return SimpleNamespace(message_manager=mm, controller=controller,
                           llm_provider=provider, _role="root")


def _text(m):
    return m.content if isinstance(m.content, str) else str(m.content)


def _additions(mm):
    return [m for m in mm.get_messages()
            if getattr(m, "origin", None) == MessageOrigin.TOOL_ADDITION]


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.delenv("TOOL_SCHEMAS_FROZEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_DEFERRED_TOOLS", raising=False)


def test_one_message_names_every_late_action_once():
    mm = _mm()
    agent = _agent(mm, late_names=("late_echo", "late_ping"))
    refresh_tool_catalog(agent)
    pushed = _additions(mm)
    assert len(pushed) == 1
    assert _text(pushed[0]) == (
        "<tool-addition>\nlate_echo\nlate_ping\n</tool-addition>")


def test_a_second_refresh_with_nothing_new_pushes_nothing():
    mm = _mm()
    agent = _agent(mm, late_names=("late_echo",))
    refresh_tool_catalog(agent)
    refresh_tool_catalog(agent)
    refresh_tool_catalog(agent)
    assert len(_additions(mm)) == 1


def test_a_newly_registered_action_is_announced_on_its_own():
    mm = _mm()
    late = ["late_echo"]
    agent = _agent(mm, late_names=late)
    agent.controller.registry.late_action_names = lambda: tuple(late)
    refresh_tool_catalog(agent)
    late.append("late_ping")
    refresh_tool_catalog(agent)
    pushed = _additions(mm)
    assert len(pushed) == 2
    assert _text(pushed[1]) == "<tool-addition>\nlate_ping\n</tool-addition>"


def test_nothing_is_pushed_in_grow_mode(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_DEFERRED_TOOLS", "false")
    mm = _mm()
    refresh_tool_catalog(_agent(mm, late_names=("late_echo",)))
    assert _additions(mm) == []


def test_nothing_is_pushed_in_bridge_mode(monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    mm = _mm()
    refresh_tool_catalog(_agent(mm, late_names=("late_echo",)))
    assert _additions(mm) == []


def test_nothing_is_pushed_on_a_non_anthropic_provider():
    mm = _mm(model="gpt-4o")
    refresh_tool_catalog(_agent(mm, late_names=("late_echo",), provider="openai"))
    assert _additions(mm) == []


def test_nothing_is_pushed_on_a_model_without_the_capability():
    """Opus 4.8 and older do not accept mid-conversation tool changes."""
    mm = _mm(model="claude-opus-4-8")
    refresh_tool_catalog(_agent(mm, late_names=("late_echo",)))
    assert _additions(mm) == []


def test_the_announcement_survives_a_persistence_round_trip(tmp_path: Path):
    """It is an ordinary history message — F13's replay must not drop it."""
    mm = _mm()
    refresh_tool_catalog(_agent(mm, late_names=("late_echo", "late_ping")))
    checkpoint = tmp_path / "history.json"
    mm.checkpoint_history(checkpoint)

    restored = _mm()
    assert restored.restore_from_checkpoint_file(checkpoint) is True
    pushed = _additions(restored)
    assert len(pushed) == 1
    assert pushed[0].origin == MessageOrigin.TOOL_ADDITION
    assert _text(pushed[0]) == (
        "<tool-addition>\nlate_echo\nlate_ping\n</tool-addition>")
