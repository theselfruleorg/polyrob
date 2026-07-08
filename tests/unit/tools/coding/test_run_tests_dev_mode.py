"""WS-1 (computer-use parity): run_tests rides sandbox-dev mode at posture >= 1.

At AGENT_COMPUTE_POSTURE >= 1 an entitled session's `run_tests` must execute with
``dev_mode`` (PYTHONPATH=/install etc.) and resolve a DEV persistent backend —
otherwise a pytest installed into /install by run_code(packages=[...]) is not
importable and the build->install->test flow still dies. Posture 0 stays
byte-identical (dev_mode False, legacy resolve shape).
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

import agents.task.constants as c
from tools.coding.tool import CodingTool, RunTestsParams


class _SpyBackend:
    def __init__(self):
        self.run_calls = []

    async def setup(self):
        pass

    async def run(self, request):
        self.run_calls.append(request)
        from tools.code_exec.result import ExecutionResult
        return ExecutionResult(stdout="ok", exit_code=0, backend="spy")

    async def teardown(self):
        pass


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "CODE_EXEC_DOCKER_PERSISTENT",
              "POLYROB_OWNER_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "true")  # bypass the server sandbox gate
    c._refreeze_compute_posture_for_tests()
    yield
    c._refreeze_compute_posture_for_tests()


def _tool(root):
    t = object.__new__(CodingTool)
    t.logger = logging.getLogger("coding-dev-test")
    t._root_override = str(root)
    t._backend = None
    t._persistent_backends = {}
    t._persistent_lock = asyncio.Lock()
    return t


def _spy_resolve(monkeypatch):
    calls = []
    backend = _SpyBackend()

    def _spy(*, session_id=None, dev_mode=False):
        calls.append({"session_id": session_id, "dev_mode": dev_mode})
        return backend

    monkeypatch.setattr("tools.code_exec.resolve_backend", _spy)
    return calls, backend


def _owner_ctx():
    return SimpleNamespace(session_id="s1", role="orchestrator", is_sub_agent=False,
                           user_id="rob", metadata={})


@pytest.mark.asyncio
async def test_run_tests_dev_mode_at_posture_1(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    c._refreeze_compute_posture_for_tests()
    calls, backend = _spy_resolve(monkeypatch)

    tool = _tool(tmp_path)
    res = await tool.run_tests(RunTestsParams(command="pytest -q"),
                               execution_context=_owner_ctx())
    assert getattr(res, "error", None) in (None, "")
    assert calls and calls[0]["dev_mode"] is True  # dev persistent backend resolved
    assert backend.run_calls[0].dev_mode is True   # request runs importable-mode


@pytest.mark.asyncio
async def test_run_tests_stays_isolated_at_posture_0(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")  # exercise the sid path
    calls, backend = _spy_resolve(monkeypatch)

    tool = _tool(tmp_path)
    res = await tool.run_tests(RunTestsParams(command="pytest -q"),
                               execution_context=_owner_ctx())
    assert getattr(res, "error", None) in (None, "")
    assert calls and calls[0]["dev_mode"] is False
    assert backend.run_calls[0].dev_mode is False
