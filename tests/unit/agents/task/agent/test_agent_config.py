"""Unit tests for AgentConfig/AgentDeps + Agent.from_params (PR9 item #2).

Pins the typed construction surface: Agent.__init__ now takes (config, deps),
and from_params splits the legacy flat kwargs into the two dataclasses with the
historical defaults preserved.
"""

from unittest.mock import MagicMock

from agents.task.agent.service import Agent, AgentConfig, AgentDeps
from agents.task.agent.prompts import SystemPrompt


def test_init_signature_is_three_params():
    # self, config, deps  — the PR9 success criterion (<= 3 params)
    import inspect
    params = list(inspect.signature(Agent.__init__).parameters)
    assert params == ["self", "config", "deps"]


def test_from_params_splits_deps_and_config(monkeypatch):
    captured = {}

    def fake_init(self, config, deps):
        captured["config"] = config
        captured["deps"] = deps

    monkeypatch.setattr(Agent, "__init__", fake_init)

    llm = MagicMock()
    orch = MagicMock()
    Agent.from_params(
        task="do a thing",
        llm=llm,
        orchestrator=orch,
        use_vision=False,
        max_actions_per_step=7,
        is_sub_agent=True,
        profile_id="p1",
    )

    c, d = captured["config"], captured["deps"]
    # config fields
    assert isinstance(c, AgentConfig) and isinstance(d, AgentDeps)
    assert c.task == "do a thing"
    assert c.use_vision is False
    assert c.max_actions_per_step == 7
    assert c.is_sub_agent is True
    assert c.profile_id == "p1"
    # deps
    assert d.llm is llm
    assert d.orchestrator is orch


def test_defaults_match_historical_init(monkeypatch):
    captured = {}
    monkeypatch.setattr(Agent, "__init__", lambda self, config, deps: captured.update(config=config, deps=deps))

    Agent.from_params(task="t", llm=MagicMock(), orchestrator=MagicMock())
    c, d = captured["config"], captured["deps"]

    # historical defaults from the old 31-param signature
    assert c.use_vision is True
    assert c.max_failures == 5
    assert c.retry_delay == 10
    assert c.max_actions_per_step == 10
    assert c.max_error_length == 400
    assert c.tool_calling_method == "auto"
    assert c.agent_name == "agent"
    assert c.use_native_tools is True
    assert c.step_timeout_seconds == 600
    assert c.max_step_timeout == 900
    assert c.is_sub_agent is False
    # deps defaults
    assert d.page_extraction_llm is None
    assert d.system_prompt_class is SystemPrompt
    assert d.register_new_step_callback is None
    assert d.register_done_callback is None


def test_typed_construction_path(monkeypatch):
    # Agent(config, deps) directly (no from_params) routes the same objects through.
    captured = {}
    monkeypatch.setattr(Agent, "__init__", lambda self, config, deps: captured.update(config=config, deps=deps))
    cfg = AgentConfig(task="t", agent_name="x")
    deps = AgentDeps(llm=MagicMock(), orchestrator=MagicMock())
    Agent(cfg, deps)
    assert captured["config"] is cfg and captured["deps"] is deps
