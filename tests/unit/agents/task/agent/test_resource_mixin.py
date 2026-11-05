"""P9 pass-9 — ResourceMixin extracted from service.py."""
from agents.task.agent.core.resources import ResourceMixin


def test_agent_composes_resource_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, ResourceMixin)
    assert Agent.get_browser_context.__qualname__.startswith("ResourceMixin")
    assert not hasattr(Agent, "cleanup_memory")  # 102 M-L: removed, it did nothing
