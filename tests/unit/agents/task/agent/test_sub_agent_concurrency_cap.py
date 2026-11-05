"""MAX_CONCURRENT_SUB_AGENTS reaches the semaphore.

The semaphore read a class constant, so the flag changed only what the CLI displayed.
"""
from types import SimpleNamespace

import pytest

from agents.task.agent.sub_agent_manager import SubAgentManager
from agents.task.constants import TimeoutConfig


def _manager():
    return SubAgentManager(SimpleNamespace(session_id="session-a"))


def test_the_semaphore_reads_the_getter(monkeypatch):
    monkeypatch.setattr(TimeoutConfig, "get_max_concurrent_sub_agents", classmethod(lambda cls: 5))
    assert _manager()._concurrency_semaphore._value == 5


@pytest.mark.parametrize("configured", [0, -2])
def test_a_low_value_cannot_block_every_sub_agent(monkeypatch, configured):
    monkeypatch.setattr(TimeoutConfig, "get_max_concurrent_sub_agents", classmethod(lambda cls: configured))
    assert _manager()._concurrency_semaphore._value == 1


def test_the_env_line_reaches_the_getter(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_SUB_AGENTS", "4")
    monkeypatch.setattr(TimeoutConfig, "_config", None)
    assert TimeoutConfig.get_max_concurrent_sub_agents() == 4
    assert _manager()._concurrency_semaphore._value == 4


def test_the_unread_constant_is_gone():
    assert not hasattr(TimeoutConfig, "MAX_CONCURRENT_SUB_AGENTS")
