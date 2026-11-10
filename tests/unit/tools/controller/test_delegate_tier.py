"""112 W2: delegate_task(tier=…, why=…) — the model names a tier, the harness resolves it."""
from types import SimpleNamespace

import pytest

import agents.task.constants  # noqa: F401 — registers the light resolver
from tools.controller import delegate_tier as dt
from tools.controller.views import DelegateTaskAction, SubtaskAction

ENV = {"DEFAULT_PROVIDER": "openrouter", "DEFAULT_MODEL": "deepseek/deepseek-v4.1-flash",
       "HEAVY_MODEL": "deepseek/deepseek-v4-pro"}
PARENT = SimpleNamespace(llm_provider="openrouter", model_name="deepseek/deepseek-v4.1-flash")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    routes = []
    monkeypatch.setattr("core.model_routing.record_model_route",
                        lambda **kw: routes.append(kw))
    return routes


def _task(text="a focused task of more than twenty chars", **kw):
    return SubtaskAction(task=text, **kw)


def test_heavy_goal_resolves_to_heavy_and_is_recorded(_env):
    r = dt.resolve_delegate_tiers(parent_agent=PARENT, model=None, provider=None, tier="heavy",
                                  why="decides a payout", tasks=None)
    assert (r.model, r.provider, r.error) == ("deepseek/deepseek-v4-pro", "openrouter", "")
    assert "goal ran on deepseek/deepseek-v4-pro (heavy)" in r.head
    assert _env[0]["tier"] == "heavy" and _env[0]["why"] == "decides a payout"


def test_default_and_light_equal_to_mine_inherit():
    for tier in ("default", "light"):
        r = dt.resolve_delegate_tiers(parent_agent=PARENT, model=None, provider=None, tier=tier,
                                      why="bulk read", tasks=None)
        assert r.model is None and not r.error


def test_unset_heavy_refuses(monkeypatch):
    monkeypatch.delenv("HEAVY_MODEL")
    r = dt.resolve_delegate_tiers(parent_agent=PARENT, model=None, provider=None, tier="heavy",
                                  why="w", tasks=None)
    assert "not configured" in r.error


def test_a_third_heavy_task_refuses_the_call():
    tasks = [_task(tier="heavy", why="w") for _ in range(3)]
    r = dt.resolve_delegate_tiers(parent_agent=PARENT, model=None, provider=None, tier=None,
                                  why=None, tasks=tasks)
    assert "at most 2 heavy" in r.error


def test_a_raw_heavy_model_counts_toward_the_heavy_cap():
    heavy = ENV["HEAVY_MODEL"]
    tasks = [_task(model=heavy), _task(model=heavy), _task(tier="heavy", why="w")]
    r = dt.resolve_delegate_tiers(parent_agent=PARENT, model=None, provider=None, tier=None,
                                  why=None, tasks=tasks)
    assert "at most 2 heavy" in r.error and "(3 asked)" in r.error
    r = dt.resolve_delegate_tiers(parent_agent=PARENT, model=heavy, provider="openrouter",
                                  tier=None, why=None, tasks=[_task(), _task(), _task()])
    assert "at most 2 heavy" in r.error
    r = dt.resolve_delegate_tiers(parent_agent=PARENT, model=heavy, provider="openrouter",
                                  tier=None, why=None, tasks=[_task(), _task()])
    assert not r.error

def test_top_level_tier_is_the_fallback_for_tasks_without_tier_or_model():
    tasks = [_task(), _task(model="some/alias-model"), _task(tier="light", why="cheap")]
    r = dt.resolve_delegate_tiers(parent_agent=PARENT, model=None, provider=None, tier="heavy",
                                  why="judge", tasks=tasks)
    assert [t.model for t in r.tasks] == ["deepseek/deepseek-v4-pro", "some/alias-model", None]
    assert r.model is None and not r.error


def test_no_tier_is_byte_identical():
    tasks = [_task(), _task()]
    r = dt.resolve_delegate_tiers(parent_agent=PARENT, model="m", provider="p", tier=None,
                                  why=None, tasks=tasks)
    assert (r.model, r.provider, r.tasks, r.head) == ("m", "p", tasks, "")


def test_schema_rules():
    with pytest.raises(ValueError):
        DelegateTaskAction(goal="x" * 25, tier="heavy")
    with pytest.raises(ValueError):
        DelegateTaskAction(goal="x" * 25, tier="heavy", why="w", model="m")
    with pytest.raises(ValueError):
        DelegateTaskAction(goal="x" * 25, tier="heavy", why="w", background=True)
    with pytest.raises(ValueError):
        DelegateTaskAction(goal="x" * 25, tier="ultra", why="w")
    with pytest.raises(ValueError):
        _task(tier="heavy")


def test_an_injected_raw_model_cannot_pass_as_a_tier():
    with pytest.raises(ValueError):
        DelegateTaskAction(goal="x" * 25, tier="anthropic/claude-opus", why="w")


def test_a_task_tier_with_a_top_level_model_is_refused():
    """112 review: an inherit-resolving task tier would run on the top-level raw model."""
    r = dt.resolve_delegate_tiers(parent_agent=PARENT, model="some/raw-model", provider=None,
                                  tier=None, why=None, tasks=[_task(tier="default", why="w")])
    assert "top-level 'model'" in r.error


def test_the_cron_money_card_names_tier_as_not_carried():
    import inspect
    import tools.cronjob_tools as cj
    assert '("tier", params.tier)' in inspect.getsource(cj.CronjobTool._money_job_card) \
        if hasattr(cj, "CronjobTool") else '("tier", params.tier)' in inspect.getsource(cj)


def test_a_refused_task_tier_records_no_route_for_the_tasks_before_it(_env, monkeypatch):
    """The call is refused as a whole, so no child ran: a record for task[0] was a
    route that never happened."""
    monkeypatch.delenv("HEAVY_MODEL")
    tasks = [_task(tier="light", why="cheap"), _task(tier="heavy", why="w")]
    r = dt.resolve_delegate_tiers(parent_agent=PARENT, model=None, provider=None, tier=None,
                                  why=None, tasks=tasks)
    assert r.error.startswith("task[1]")
    assert _env == []


def test_deferred_records_wait_for_commit(_env):
    """``defer_record`` lets the caller record only after the child model is built."""
    r = dt.resolve_delegate_tiers(parent_agent=PARENT, model=None, provider=None, tier="heavy",
                                  why="decides a payout", tasks=None, defer_record=True)
    assert r.model == ENV["HEAVY_MODEL"] and _env == []
    r.commit_records()
    r.commit_records()                       # once only
    assert [e["tier"] for e in _env] == ["heavy"]


def _delegate_controller(monkeypatch, build):
    """A bare Controller whose parent agent builds a child model with *build*."""
    import logging
    import agents.task.agent.service  # noqa: F401 — controller<->orchestrator import order
    import agents.task.model_switch_service as svc
    from agents.task.constants import TimeoutConfig
    from tools.controller.registry.service import Registry
    from tools.controller.service import Controller

    async def _allow(parent_agent, model, provider):
        return ""
    monkeypatch.setattr(svc, "child_model_refusal", _allow)
    monkeypatch.setattr(TimeoutConfig, "get_sub_agents_enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(TimeoutConfig, "get_max_sub_agent_depth", classmethod(lambda cls: 1))

    class _Mgr:
        async def run_subtask(self, **kw):
            return SimpleNamespace(success=True, output="ok", error=None, finished=True)

    parent = SimpleNamespace(llm_provider="openrouter", model_name=ENV["DEFAULT_MODEL"],
                             _create_llm_from_config_async=build)
    c = object.__new__(Controller)
    c.logger = logging.getLogger("delegate-tier-route")
    c.registry = Registry()
    c.orchestrator = SimpleNamespace(agents={"main": parent}, sub_agent_manager=_Mgr(),
                                     session_id="sess1", user_id="u1")
    c._register_subtask_action()
    return c.registry.registry.actions["delegate_task"].function


@pytest.mark.parametrize("builds", [True, False])
def test_a_route_is_recorded_only_when_the_child_model_is_built(_env, monkeypatch, builds):
    """/status must show no heavy route for a child that never ran."""
    import asyncio
    from agents.task.loop_types.execution_context import ActionExecutionContext

    async def build(cfg, isolated=False):
        return object() if builds else None

    fn = _delegate_controller(monkeypatch, build)
    res = asyncio.run(fn(DelegateTaskAction(goal="decide the payout split for the quarter",
                                            tier="heavy", why="decides a payout"),
                         execution_context=ActionExecutionContext(
                             agent_id="main", is_sub_agent=False, role="orchestrator")))
    if builds:
        assert res.error is None and [e["tier"] for e in _env] == ["heavy"]
    else:
        assert res.error and _env == []
