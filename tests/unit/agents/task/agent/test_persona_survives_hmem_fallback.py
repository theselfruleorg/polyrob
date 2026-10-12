"""The H-MEM fallback (no memory_manager in the container) must not replace the
AgentConfig that _init_message_manager reads persona_block from — the chat persona
was silently dropped whenever a dedicated TaskContextManager was built."""
import logging
import types

from agents.task.agent.service import Agent, AgentConfig


class _Container:
    config = types.SimpleNamespace(data_dir=None)

    def get_service(self, name):
        return None


def test_agent_without_memory_manager_keeps_the_persona_block(monkeypatch):
    import modules.memory.task.task_context_manager as tcm
    monkeypatch.setattr(tcm, "TaskContextManager", lambda **kw: object())
    a = object.__new__(Agent)
    a.logger = logging.getLogger("persona-fallback")
    a._is_sub_agent = False
    a.orchestrator = types.SimpleNamespace(container=_Container(), session_id="s1",
                                           user_id="u1")
    cfg = AgentConfig(task="t", persona_block="I am Rob.")
    p = types.SimpleNamespace(config=cfg)
    a._init_task_memory(p)
    assert a._created_task_context_manager is True   # the fallback path ran
    assert p.config is cfg
    assert getattr(p.config, "persona_block", None) == "I am Rob."
