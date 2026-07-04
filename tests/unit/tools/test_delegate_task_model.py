"""B4 — optional per-sub-agent model on `delegate_task` (+ SubtaskAction.model).

Mirrors the bare-controller/fake-orchestrator approach in test_delegate_task_action.py.
Adds a fake parent agent exposing `_create_llm_from_config_async` (the real
`LLMProvisioningMixin` seam) so we can assert exactly what config `_delegate`
builds the child LLM with, without spinning up a real LLM/container.
"""
import logging
import types

import pytest
from pydantic import ValidationError

import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
from tools.controller.service import Controller
from tools.controller.registry.service import Registry
from tools.controller.execution_context import ActionExecutionContext
from tools.controller.views import DelegateTaskAction, SubtaskAction


# --- fakes -------------------------------------------------------------------

class _Result:
    def __init__(self, success=True, output="done", error=None):
        self.success = success
        self.output = output
        self.error = error


class _FakeManager:
    def __init__(self):
        self.run_subtask_calls = []
        self.run_parallel_calls = []

    async def run_subtask(self, **kwargs):
        self.run_subtask_calls.append(kwargs)
        return _Result(success=True, output="goal-result")

    async def run_parallel_subtasks(self, **kwargs):
        self.run_parallel_calls.append(kwargs)
        n = len(kwargs.get("subtasks", []))
        return [_Result(success=True) for _ in range(n)]

    def format_results_for_prompt(self, results):
        return "formatted-results"


_SENTINEL_LLM = object()


class _FakeParentAgent:
    """Stands in for the real Agent object's LLMProvisioningMixin seam."""

    def __init__(self, llm=_SENTINEL_LLM, fail=False, build_none=False):
        self.calls = []  # list of {"cfg": ..., "isolated": ...}
        self._llm = llm
        self._fail = fail
        self._build_none = build_none

    async def _create_llm_from_config_async(self, cfg, isolated=False):
        self.calls.append({"cfg": dict(cfg), "isolated": isolated})
        if self._fail:
            raise RuntimeError("boom")
        if self._build_none:
            return None
        return self._llm


def _controller(monkeypatch, parent_agent=None, max_depth=1):
    from agents.task.constants import TimeoutConfig
    monkeypatch.setattr(TimeoutConfig, "get_sub_agents_enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(TimeoutConfig, "get_max_sub_agent_depth", classmethod(lambda cls: max_depth))
    c = object.__new__(Controller)
    c.logger = logging.getLogger("delegate-model-test")
    c.registry = Registry()
    mgr = _FakeManager()
    agents = {"main": parent_agent if parent_agent is not None else object()}
    c.orchestrator = types.SimpleNamespace(agents=agents, sub_agent_manager=mgr, session_id="sess1")
    c._fake_mgr = mgr
    return c


def _delegate_fn(c: Controller):
    return c.registry.registry.actions["delegate_task"].function


def _subtask_fn(c: Controller):
    return c.registry.registry.actions["subtask"].function


def _ctx(role="orchestrator", is_sub=False):
    return ActionExecutionContext(agent_id="main", is_sub_agent=is_sub, role=role)


# --- pydantic validation -------------------------------------------------------

def test_delegate_task_action_accepts_model_and_provider():
    a = DelegateTaskAction(goal="research the topic in significant depth please",
                            model="claude-haiku-4-5", provider="anthropic")
    assert a.model == "claude-haiku-4-5"
    assert a.provider == "anthropic"


def test_delegate_task_action_model_defaults_to_none():
    a = DelegateTaskAction(goal="research the topic in significant depth please")
    assert a.model is None
    assert a.provider is None


def test_delegate_task_action_still_rejects_unknown_extra_field():
    with pytest.raises(ValidationError):
        DelegateTaskAction(goal="research the topic in significant depth please", bogus_field=1)


def test_subtask_action_accepts_model_and_provider():
    a = SubtaskAction(task="a sufficiently long delegated subtask description",
                       model="claude-haiku-4-5")
    assert a.model == "claude-haiku-4-5"


def test_subtask_action_still_rejects_unknown_extra_field():
    with pytest.raises(ValidationError):
        SubtaskAction(task="a sufficiently long delegated subtask description", bogus_field=1)


# --- goal shape: model build + forwarding --------------------------------------

@pytest.mark.asyncio
async def test_goal_with_model_builds_isolated_llm_and_forwards_to_run_subtask(monkeypatch):
    parent = _FakeParentAgent()
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="research the topic in significant depth please",
                            model="claude-haiku-4-5"),
        execution_context=_ctx(),
    )
    assert res.error is None
    assert len(parent.calls) == 1
    assert parent.calls[0]["cfg"] == {"model": "claude-haiku-4-5"}
    assert parent.calls[0]["isolated"] is True
    assert c._fake_mgr.run_subtask_calls
    assert c._fake_mgr.run_subtask_calls[0]["parent_llm"] is _SENTINEL_LLM


@pytest.mark.asyncio
async def test_goal_with_model_and_provider_threads_provider_into_cfg(monkeypatch):
    parent = _FakeParentAgent()
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    await _delegate_fn(c)(
        DelegateTaskAction(goal="research the topic in significant depth please",
                            model="glm-4.6", provider="openrouter"),
        execution_context=_ctx(),
    )
    assert parent.calls[0]["cfg"] == {"model": "glm-4.6", "provider": "openrouter"}


@pytest.mark.asyncio
async def test_goal_model_build_failure_returns_error_and_skips_run_subtask(monkeypatch):
    parent = _FakeParentAgent(build_none=True)
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="research the topic in significant depth please",
                            model="totally-bogus-model"),
        execution_context=_ctx(),
    )
    assert res.error is not None
    assert "totally-bogus-model" in res.error
    assert not c._fake_mgr.run_subtask_calls


@pytest.mark.asyncio
async def test_goal_model_build_raises_returns_error_and_skips_run_subtask(monkeypatch):
    parent = _FakeParentAgent(fail=True)
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="research the topic in significant depth please",
                            model="claude-haiku-4-5"),
        execution_context=_ctx(),
    )
    assert res.error is not None
    assert not c._fake_mgr.run_subtask_calls


@pytest.mark.asyncio
async def test_goal_no_model_is_byte_identical_parent_llm_none(monkeypatch):
    parent = _FakeParentAgent()
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="research the topic in significant depth please"),
        execution_context=_ctx(),
    )
    assert res.error is None
    assert not parent.calls  # no LLM build attempted
    assert c._fake_mgr.run_subtask_calls[0]["parent_llm"] is None


# --- parallel (tasks) shape: per-task model + fallback -------------------------

@pytest.mark.asyncio
async def test_parallel_per_task_model_wins_over_top_level_fallback(monkeypatch):
    llm_a, llm_b = object(), object()
    calls_by_model = {}

    class _MultiModelParent(_FakeParentAgent):
        async def _create_llm_from_config_async(self, cfg, isolated=False):
            self.calls.append({"cfg": dict(cfg), "isolated": isolated})
            calls_by_model[cfg["model"]] = cfg
            return {"model-a": llm_a, "model-b": llm_b}[cfg["model"]]

    parent = _MultiModelParent()
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    res = await _delegate_fn(c)(
        DelegateTaskAction(
            tasks=[
                {"task": "scrape source one thoroughly please", "model": "model-a"},
                {"task": "scrape source two thoroughly please"},
            ],
            model="model-b",
        ),
        execution_context=_ctx(),
    )
    assert res.error is None
    assert len(c._fake_mgr.run_parallel_calls) == 1
    subtasks = c._fake_mgr.run_parallel_calls[0]["subtasks"]
    assert subtasks[0]["llm"] is llm_a
    assert subtasks[1]["llm"] is llm_b
    assert "model-a" in calls_by_model and "model-b" in calls_by_model


@pytest.mark.asyncio
async def test_parallel_no_model_requested_is_byte_identical(monkeypatch):
    parent = _FakeParentAgent()
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    res = await _delegate_fn(c)(
        DelegateTaskAction(tasks=[
            {"task": "scrape source one thoroughly please"},
            {"task": "scrape source two thoroughly please"},
        ]),
        execution_context=_ctx(),
    )
    assert res.error is None
    assert not parent.calls
    subtasks = c._fake_mgr.run_parallel_calls[0]["subtasks"]
    assert all(st.get("llm") is None for st in subtasks)


@pytest.mark.asyncio
async def test_parallel_model_build_failure_aborts_before_dispatch(monkeypatch):
    parent = _FakeParentAgent(build_none=True)
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    res = await _delegate_fn(c)(
        DelegateTaskAction(tasks=[
            {"task": "scrape source one thoroughly please", "model": "bad-model"},
            {"task": "scrape source two thoroughly please"},
        ]),
        execution_context=_ctx(),
    )
    assert res.error is not None
    assert "bad-model" in res.error
    assert not c._fake_mgr.run_parallel_calls  # aborted before dispatch


# --- background + model: v1 clean rejection ------------------------------------

@pytest.mark.asyncio
async def test_background_with_model_is_rejected_cleanly(monkeypatch):
    parent = _FakeParentAgent()
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    class _FakeAsyncDelegation:
        def __init__(self):
            self.dispatched = []

        async def dispatch(self, **kwargs):
            self.dispatched.append(kwargs)
            return {"status": "dispatched", "delegation_id": "deleg_0001"}

    fake_async = _FakeAsyncDelegation()
    c.orchestrator.async_delegation = fake_async

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="research this topic in the background please",
                            background=True, model="claude-haiku-4-5"),
        execution_context=_ctx(),
    )
    assert res.error is not None
    assert "background" in res.error.lower()
    assert not fake_async.dispatched
    assert not c._fake_mgr.run_subtask_calls
    assert not parent.calls  # never even attempted the build


# --- B4 validator: provider without model is rejected, not silently ignored ----

@pytest.mark.asyncio
async def test_goal_provider_without_model_is_rejected(monkeypatch):
    parent = _FakeParentAgent()
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="research the topic in significant depth please",
                            provider="anthropic"),  # provider, no model
        execution_context=_ctx(),
    )
    assert res.error is not None
    assert "provider" in res.error.lower() and "model" in res.error.lower()
    assert not parent.calls               # never attempted a build
    assert not c._fake_mgr.run_subtask_calls  # never dispatched


@pytest.mark.asyncio
async def test_parallel_task_provider_without_model_is_rejected(monkeypatch):
    parent = _FakeParentAgent()
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    res = await _delegate_fn(c)(
        DelegateTaskAction(tasks=[
            {"task": "scrape source one thoroughly please", "provider": "anthropic"},  # no model
            {"task": "scrape source two thoroughly please"},
        ]),
        execution_context=_ctx(),
    )
    assert res.error is not None
    assert "task[0]" in res.error
    assert not parent.calls
    assert not c._fake_mgr.run_parallel_calls


@pytest.mark.asyncio
async def test_goal_provider_with_model_still_ok(monkeypatch):
    # Guard must NOT reject the valid paired case.
    parent = _FakeParentAgent()
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="research the topic in significant depth please",
                            model="glm-4.6", provider="openrouter"),
        execution_context=_ctx(),
    )
    assert res.error is None
    assert parent.calls[0]["cfg"] == {"model": "glm-4.6", "provider": "openrouter"}


# --- legacy shims: must not crash now that SubtaskAction carries model/provider -

@pytest.mark.asyncio
async def test_legacy_subtask_with_model_field_does_not_crash(monkeypatch):
    """v1: the deprecated singular `subtask` verb doesn't need to honor `model` —
    it just must not crash now that SubtaskAction has the field."""
    parent = _FakeParentAgent()
    c = _controller(monkeypatch, parent_agent=parent)
    c._register_subtask_action()

    res = await _subtask_fn(c)(
        SubtaskAction(task="a sufficiently long delegated subtask description",
                      model="claude-haiku-4-5"),
        execution_context=_ctx(),
    )
    assert res.error is None
    assert c._fake_mgr.run_subtask_calls
