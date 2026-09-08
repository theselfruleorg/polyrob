"""Fixtures for the app_service suite (mirrors tests/unit/tools/hf_deploy/conftest.py)."""
import types

import pytest


@pytest.fixture
def owner_ctx(monkeypatch):
    """An owner-tenant orchestrator turn (no posture needed — the app tool states
    the three publish clauses directly)."""
    from tools.controller.execution_context import ActionExecutionContext
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    return ActionExecutionContext(session_id="sess-app", user_id="owner-1",
                                  role="orchestrator", is_sub_agent=False)


@pytest.fixture
def app_env(monkeypatch):
    monkeypatch.setenv("APP_SERVICE_ENABLED", "true")
    monkeypatch.delenv("AGENT_BUILDER_MODE", raising=False)
    yield


class _FakeAction:
    def __init__(self, name):
        self._name = name

    def model_dump(self, exclude_unset=True):
        return {self._name: {}}


class GreenLedgerOrch:
    """Fake orchestrator whose ledger shows the given (action, error) entries."""

    def __init__(self, entries):
        agent = types.SimpleNamespace(_is_sub_agent=False)
        steps = []
        for name, error in entries:
            result = types.SimpleNamespace(error=error, extracted_content="ok")
            step = types.SimpleNamespace(
                model_output=types.SimpleNamespace(action=[_FakeAction(name)]),
                result=[result],
            )
            steps.append(step)
        agent.history = types.SimpleNamespace(history=steps)
        self.agents = {"a1": agent}


@pytest.fixture
def green_orch():
    return GreenLedgerOrch([("str_replace", None), ("run_tests", None)])


@pytest.fixture
def no_green_orch():
    return GreenLedgerOrch([("str_replace", None), ("run_tests", "1 failed")])
