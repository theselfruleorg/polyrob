"""P9 pass-26 — AgentConstructionMixin (__init__) split out of service.py (-> <700)."""


def test_agent_inherits_init_from_construction_mixin():
    from agents.task.agent.service import Agent
    from agents.task.agent.core.construction import AgentConstructionMixin
    assert issubclass(Agent, AgentConstructionMixin)
    assert Agent.__init__.__qualname__.startswith("AgentConstructionMixin")


def test_from_params_still_in_agent():
    from agents.task.agent.service import Agent, AgentConfig, AgentDeps
    assert hasattr(Agent, "from_params")
    assert AgentConfig is not None and AgentDeps is not None
