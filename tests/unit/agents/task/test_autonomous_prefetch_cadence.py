"""SA-06 (2026-07-06 structural review): server autonomous runs recalled memory
once at step 1 with a task-only query (cadence default 0 off-local) — and the
step-1 brain enrichment is dead because next_goal is empty there. Autonomous
sessions now default to cadence 3 (like local); an explicit env still wins.
"""
from agents.task.constants import memory_prefetch_cadence


def test_autonomous_defaults_to_cadence_3(monkeypatch):
    monkeypatch.delenv("MEMORY_PREFETCH_CADENCE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    assert memory_prefetch_cadence() == 0                      # server chat: unchanged
    assert memory_prefetch_cadence(autonomous=True) == 3       # server autonomous: recurring


def test_explicit_env_wins_even_for_autonomous(monkeypatch):
    monkeypatch.setenv("MEMORY_PREFETCH_CADENCE", "0")
    assert memory_prefetch_cadence(autonomous=True) == 0
    monkeypatch.setenv("MEMORY_PREFETCH_CADENCE", "7")
    assert memory_prefetch_cadence(autonomous=True) == 7


def test_prefetch_mixin_passes_autonomy():
    import inspect

    from agents.task.agent.core import memory_prefetch

    src = inspect.getsource(memory_prefetch.MemoryPrefetchMixin._maybe_prefetch_memory)
    assert "autonomous" in src


def test_prefetch_skips_sub_agents():
    """P2-4: a delegated leaf sub-agent gets no automatic cross-session recall."""
    import asyncio
    from agents.task.agent.core.memory_prefetch import MemoryPrefetchMixin

    class _Agent(MemoryPrefetchMixin):
        def __init__(self):
            self._is_sub_agent = True
            self.session_id = "s1"
            self.state = type("S", (), {"n_steps": 1})()
            self.pushed = []

        class _MM:
            def push_ephemeral_message(self, m):  # pragma: no cover - must not be called
                raise AssertionError("sub-agent must not prefetch memory")

        message_manager = _MM()

    a = _Agent()
    # must return without pushing an ephemeral recall message
    asyncio.run(a._maybe_prefetch_memory())


def test_prefetch_guard_matches_other_injectors():
    """The sub-agent guard mirrors the episodic/continuity injectors."""
    import inspect
    from agents.task.agent.core import memory_prefetch
    src = inspect.getsource(memory_prefetch.MemoryPrefetchMixin._maybe_prefetch_memory)
    assert '_is_sub_agent' in src
