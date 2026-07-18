"""Shared fixtures for the hf_deploy suite.

Every test that touches the deployed_apps registry passes an explicit tmp
``db_path`` (belt) — the global conftest additionally redirects
``DEPLOYED_APPS_DB_PATH`` to a per-test tmp dir (suspenders), so unit runs can
never write the developer's real data home (the autonomy-state landmine).
"""
import types

import pytest

import agents.task.constants as _constants


@pytest.fixture
def owner_ctx(monkeypatch):
    """An execution context that PASSES compute_posture_allows(ctx, 2):
    posture frozen to 2, owner principal matches, orchestrator role, not a
    sub-agent, no forged turn_kind stamp."""
    from tools.controller.execution_context import ActionExecutionContext

    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    _constants._refreeze_compute_posture_for_tests()
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    ctx = ActionExecutionContext(
        session_id="sess-hf", user_id="owner-1", role="orchestrator",
        is_sub_agent=False,
    )
    yield ctx
    monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
    import os
    os.environ.pop("AGENT_COMPUTE_POSTURE", None)
    _constants._refreeze_compute_posture_for_tests()


@pytest.fixture
def deploy_env(monkeypatch):
    """Minimal env for a deployable host: org + token configured."""
    monkeypatch.setenv("HF_DEPLOY_ORG", "test-org")
    monkeypatch.setenv("HF_TOKEN", "hf_SECRETTOKENVALUE123")
    yield "hf_SECRETTOKENVALUE123"


class FakeSpaceRuntime:
    def __init__(self, stage="RUNNING"):
        self.stage = stage


class FakeHfApi:
    """Records calls; injectable via HFSpacesBroker(api_factory=...)."""

    def __init__(self, token, *, stages=None, fail_upload=None):
        self.token = token
        self.calls = []
        self._stages = list(stages or ["RUNNING"])
        self._fail_upload = fail_upload

    def create_repo(self, repo_id, repo_type=None, space_sdk=None, exist_ok=None, private=None):
        self.calls.append(("create_repo", repo_id, repo_type, space_sdk))

    def upload_folder(self, repo_id=None, repo_type=None, folder_path=None):
        if self._fail_upload is not None:
            raise self._fail_upload
        self.calls.append(("upload_folder", repo_id, repo_type, folder_path))

    def add_space_secret(self, repo_id=None, key=None, value=None):
        self.calls.append(("add_space_secret", repo_id, key, value))

    def get_space_runtime(self, repo_id):
        stage = self._stages.pop(0) if len(self._stages) > 1 else self._stages[0]
        self.calls.append(("get_space_runtime", repo_id, stage))
        return FakeSpaceRuntime(stage)

    def delete_repo(self, repo_id=None, repo_type=None):
        self.calls.append(("delete_repo", repo_id, repo_type))


class _FakeAction:
    """Shape read by run_outcome._action_name (model_dump -> first key)."""

    def __init__(self, name):
        self._name = name

    def model_dump(self, exclude_unset=True):
        return {self._name: {}}


class GreenLedgerOrch:
    """Fake orchestrator whose ledger shows one successful run_tests and no
    later edit — the shape _walk_ledger + edited_since_last_test read."""

    def __init__(self, entries):
        # entries: list of (action_name, error_or_None)
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
def edited_after_test_orch():
    return GreenLedgerOrch([("run_tests", None), ("str_replace", None)])


@pytest.fixture
def no_green_orch():
    return GreenLedgerOrch([("str_replace", None), ("run_tests", "1 failed")])
