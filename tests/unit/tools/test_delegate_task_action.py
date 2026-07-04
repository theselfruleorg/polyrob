"""P1 — delegate_task action: registration gating + goal/tasks routing.

Builds a bare Controller with a real Registry and a fake orchestrator/manager so
we exercise the registered closure without spinning up the full agent stack
(mirrors the bare-controller approach used for the pre_tool_call hook tests).
"""
import logging
import types

import pytest

import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
from tools.controller.service import Controller
from tools.controller.registry.service import Registry
from tools.controller.execution_context import ActionExecutionContext


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
        return [_Result(success=True), _Result(success=True)]

    def format_results_for_prompt(self, results):
        return "formatted-results"


def _controller(enabled: bool, monkeypatch) -> Controller:
    # The BotConfig singleton overrides env, so patch the gate classmethods directly.
    from agents.task.constants import TimeoutConfig
    monkeypatch.setattr(TimeoutConfig, "get_sub_agents_enabled", classmethod(lambda cls: enabled))
    monkeypatch.setattr(TimeoutConfig, "get_max_sub_agent_depth", classmethod(lambda cls: 1))
    c = object.__new__(Controller)
    c.logger = logging.getLogger("delegate-test")
    c.registry = Registry()
    mgr = _FakeManager()
    c.orchestrator = types.SimpleNamespace(agents={"main": object()}, sub_agent_manager=mgr)
    c._fake_mgr = mgr  # for assertions
    return c


def _delegate_fn(c: Controller):
    return c.registry.registry.actions["delegate_task"].function


def _ctx(role="orchestrator", is_sub=False):
    return ActionExecutionContext(agent_id="main", is_sub_agent=is_sub, role=role)


# --- registration gating -----------------------------------------------------

def test_not_registered_when_disabled(monkeypatch):
    c = _controller(enabled=False, monkeypatch=monkeypatch)
    c._register_subtask_action()
    assert "delegate_task" not in c.registry.registry.actions


def test_registered_when_enabled(monkeypatch):
    c = _controller(enabled=True, monkeypatch=monkeypatch)
    c._register_subtask_action()
    assert "delegate_task" in c.registry.registry.actions


# --- routing -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_goal_routes_to_run_subtask(monkeypatch):
    c = _controller(enabled=True, monkeypatch=monkeypatch)
    c._register_subtask_action()
    from tools.controller.views import DelegateTaskAction

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="research the topic in significant depth please"),
        execution_context=_ctx(),
    )
    assert c._fake_mgr.run_subtask_calls and not c._fake_mgr.run_parallel_calls
    assert res.error is None
    assert "completed" in (res.extracted_content or "").lower()


@pytest.mark.asyncio
async def test_tasks_routes_to_parallel(monkeypatch):
    c = _controller(enabled=True, monkeypatch=monkeypatch)
    c._register_subtask_action()
    from tools.controller.views import DelegateTaskAction

    res = await _delegate_fn(c)(
        DelegateTaskAction(tasks=[
            {"task": "scrape source one thoroughly please"},
            {"task": "scrape source two thoroughly please"},
        ]),
        execution_context=_ctx(),
    )
    assert c._fake_mgr.run_parallel_calls and not c._fake_mgr.run_subtask_calls
    assert "2/2 succeeded" in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_leaf_caller_denied_without_spawning(monkeypatch):
    c = _controller(enabled=True, monkeypatch=monkeypatch)
    c._register_subtask_action()
    from tools.controller.views import DelegateTaskAction

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="some sufficiently long delegated goal text"),
        execution_context=_ctx(role="leaf", is_sub=True),
    )
    assert res.error  # denied
    assert not c._fake_mgr.run_subtask_calls
    assert not c._fake_mgr.run_parallel_calls


# --- UP-10 2.4: legacy verbs now also enforce the role/depth gate -------------

def _action_fn(c: Controller, name: str):
    return c.registry.registry.actions[name].function


@pytest.mark.asyncio
async def test_legacy_subtask_leaf_denied(monkeypatch):
    """A leaf caller hitting the deprecated `subtask` verb is now refused (was
    allowed by the old inline check, which had no role concept)."""
    c = _controller(enabled=True, monkeypatch=monkeypatch)
    c._register_subtask_action()
    from tools.controller.views import SubtaskAction

    res = await _action_fn(c, "subtask")(
        SubtaskAction(task="a sufficiently long delegated subtask description"),
        execution_context=_ctx(role="leaf", is_sub=True),
    )
    assert res.error  # denied via evaluate_delegation
    assert not c._fake_mgr.run_subtask_calls


@pytest.mark.asyncio
async def test_legacy_subtask_orchestrator_spawns(monkeypatch):
    c = _controller(enabled=True, monkeypatch=monkeypatch)
    c._register_subtask_action()
    from tools.controller.views import SubtaskAction

    res = await _action_fn(c, "subtask")(
        SubtaskAction(task="a sufficiently long delegated subtask description"),
        execution_context=_ctx(role="orchestrator"),
    )
    assert res.error is None
    assert c._fake_mgr.run_subtask_calls


@pytest.mark.asyncio
async def test_legacy_parallel_leaf_denied(monkeypatch):
    c = _controller(enabled=True, monkeypatch=monkeypatch)
    c._register_subtask_action()
    from tools.controller.views import ParallelSubtasksAction

    res = await _action_fn(c, "parallel_subtasks")(
        ParallelSubtasksAction(subtasks=[
            {"task": "scrape source one thoroughly please"},
            {"task": "scrape source two thoroughly please"},
        ]),
        execution_context=_ctx(role="leaf", is_sub=True),
    )
    assert res.error  # denied via evaluate_delegation
    assert not c._fake_mgr.run_parallel_calls


# --- UP-12: background delegation handler branch ----------------------------

class _FakeAsyncDelegation:
    def __init__(self, reject=False):
        self.reject = reject
        self.dispatched = []

    async def dispatch(self, **kwargs):
        self.dispatched.append(kwargs)
        if self.reject:
            return {"status": "rejected", "error": "at capacity"}
        return {"status": "dispatched", "delegation_id": "deleg_0001"}


@pytest.mark.asyncio
async def test_background_goal_dispatches_without_blocking(monkeypatch):
    c = _controller(enabled=True, monkeypatch=monkeypatch)
    c._register_subtask_action()
    fake = _FakeAsyncDelegation()
    c.orchestrator.async_delegation = fake
    from tools.controller.views import DelegateTaskAction

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="research this topic in the background please", background=True),
        execution_context=_ctx(),
    )
    assert fake.dispatched and not c._fake_mgr.run_subtask_calls  # detached, parent not blocked
    assert "deleg_0001" in (res.extracted_content or "")
    assert "background" in (res.extracted_content or "").lower()


@pytest.mark.asyncio
async def test_background_capacity_rejection_surfaces_error(monkeypatch):
    c = _controller(enabled=True, monkeypatch=monkeypatch)
    c._register_subtask_action()
    c.orchestrator.async_delegation = _FakeAsyncDelegation(reject=True)
    from tools.controller.views import DelegateTaskAction

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="research this topic in the background please", background=True),
        execution_context=_ctx(),
    )
    assert res.error and "capacity" in res.error


@pytest.mark.asyncio
async def test_background_leaf_denied_before_dispatch(monkeypatch):
    c = _controller(enabled=True, monkeypatch=monkeypatch)
    c._register_subtask_action()
    fake = _FakeAsyncDelegation()
    c.orchestrator.async_delegation = fake
    from tools.controller.views import DelegateTaskAction

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="research this topic in the background please", background=True),
        execution_context=_ctx(role="leaf", is_sub=True),
    )
    assert res.error  # denied by evaluate_delegation
    assert not fake.dispatched  # no task created


@pytest.mark.asyncio
async def test_sync_goal_unchanged_when_background_false(monkeypatch):
    c = _controller(enabled=True, monkeypatch=monkeypatch)
    c._register_subtask_action()
    c.orchestrator.async_delegation = _FakeAsyncDelegation()
    from tools.controller.views import DelegateTaskAction

    res = await _delegate_fn(c)(
        DelegateTaskAction(goal="research this topic synchronously please thanks"),
        execution_context=_ctx(),
    )
    assert c._fake_mgr.run_subtask_calls  # ran synchronously
    assert "completed" in (res.extracted_content or "").lower()


def test_prompt_teaches_only_delegate_task():
    """The subtask section no longer documents the legacy verbs as call syntax."""
    from agents.task.agent.prompts import SystemPrompt
    from agents.task.constants import TimeoutConfig
    # Build a minimal instance just to call _get_subtask_section with sub-agents on.
    inst = object.__new__(SystemPrompt)
    orig = TimeoutConfig.get_sub_agents_enabled
    try:
        TimeoutConfig.get_sub_agents_enabled = classmethod(lambda cls: True)
        section = inst._get_subtask_section()
    finally:
        TimeoutConfig.get_sub_agents_enabled = orig
    assert "delegate_task(" in section
    assert "subtask(task" not in section
    assert "parallel_subtasks(subtasks" not in section
