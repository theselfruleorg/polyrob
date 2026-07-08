"""WS-2: the ShellTool `shell_run` action — gating, state persistence, discipline.

The tool is posture-gated (compute_posture_allows(ctx, 1)); an unentitled session
gets a clear denial. An entitled session's cwd/env persist across calls; a
foreground server command is nudged to background; a background command registers a
job and returns its id.
"""
import asyncio
import logging

import pytest

import agents.task.constants as c
from tools.shell.tool import ShellTool, ShellRunParams
from tools.shell.state import ShellState, STATE_SENTINEL
from tools.shell.process_registry import ProcessRegistry
from tools.code_exec.result import ExecutionResult
from tools.controller.execution_context import ActionExecutionContext


class _FakeBackend:
    def __init__(self):
        self.runs = []
        self.detached = []
        self._responses = []

    def push(self, result):
        self._responses.append(result)

    async def setup(self):
        pass

    async def run(self, request):
        self.runs.append(request)
        if self._responses:
            return self._responses.pop(0)
        return ExecutionResult(stdout="", exit_code=0, backend="fake")

    async def exec_detached(self, script):
        self.detached.append(script)
        return 0


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "POLYROB_LOCAL", "POLYROB_OWNER_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    c._refreeze_compute_posture_for_tests()
    yield
    c._refreeze_compute_posture_for_tests()


def _tool(backend):
    t = object.__new__(ShellTool)
    t.logger = logging.getLogger("shell-tool-test")
    t._registry = ProcessRegistry()
    t._states = {}
    t._lock = asyncio.Lock()
    async def _fake_executor(execution_context):
        from tools.shell.executor import DockerShellExecutor
        return DockerShellExecutor(backend)
    t._resolve_executor = _fake_executor
    return t


def _owner_ctx(**kw):
    d = dict(role="orchestrator", is_sub_agent=False, user_id="rob",
             session_id="s1", metadata={"turn_kind": None})
    d.update(kw)
    return ActionExecutionContext(**d)


def _posture(monkeypatch, val):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", val)
    c._refreeze_compute_posture_for_tests()


@pytest.mark.asyncio
async def test_denied_at_posture_0():
    be = _FakeBackend()
    t = _tool(be)
    res = await t.shell_run(ShellRunParams(command="ls"), execution_context=_owner_ctx())
    assert res.error and "posture" in res.error.lower()
    assert be.runs == []


@pytest.mark.asyncio
async def test_denied_for_leaf_even_at_posture_1(monkeypatch):
    _posture(monkeypatch, "1")
    be = _FakeBackend()
    t = _tool(be)
    res = await t.shell_run(ShellRunParams(command="ls"),
                            execution_context=_owner_ctx(role="leaf"))
    assert res.error and be.runs == []


@pytest.mark.asyncio
async def test_foreground_runs_and_returns_output(monkeypatch):
    _posture(monkeypatch, "1")
    be = _FakeBackend()
    be.push(ExecutionResult(
        stdout=f"hello\n{STATE_SENTINEL}\x1e__CWD__\x1e/workspace\n\x1e__ENV__\x1e\n",
        exit_code=0, backend="fake"))
    t = _tool(be)
    res = await t.shell_run(ShellRunParams(command="echo hello"),
                            execution_context=_owner_ctx())
    assert not res.error
    assert "hello" in res.extracted_content


@pytest.mark.asyncio
async def test_cwd_persists_across_calls(monkeypatch):
    _posture(monkeypatch, "1")
    be = _FakeBackend()
    be.push(ExecutionResult(
        stdout=f"\n{STATE_SENTINEL}\x1e__CWD__\x1e/workspace/proj\n\x1e__ENV__\x1e\n",
        exit_code=0, backend="fake"))
    be.push(ExecutionResult(
        stdout=f"/workspace/proj\n{STATE_SENTINEL}\x1e__CWD__\x1e/workspace/proj\n\x1e__ENV__\x1e\n",
        exit_code=0, backend="fake"))
    t = _tool(be)
    ctx = _owner_ctx()
    await t.shell_run(ShellRunParams(command="mkdir proj && cd proj"), execution_context=ctx)
    # second call's wrapped script must cd into the persisted cwd
    await t.shell_run(ShellRunParams(command="pwd"), execution_context=ctx)
    assert "cd /workspace/proj" in be.runs[1].code


@pytest.mark.asyncio
async def test_foreground_server_command_is_nudged(monkeypatch):
    _posture(monkeypatch, "1")
    be = _FakeBackend()
    t = _tool(be)
    res = await t.shell_run(ShellRunParams(command="flask run"),
                            execution_context=_owner_ctx())
    assert res.error and "background" in res.error.lower()
    assert be.runs == [] and be.detached == []


@pytest.mark.asyncio
async def test_background_registers_job_and_detaches(monkeypatch):
    _posture(monkeypatch, "1")
    be = _FakeBackend()
    t = _tool(be)
    res = await t.shell_run(ShellRunParams(command="flask run", background=True),
                            execution_context=_owner_ctx())
    assert not res.error
    assert be.detached, "background must use a detached exec"
    jobs = t._registry.list("s1")
    assert len(jobs) == 1
    assert jobs[0].id in res.extracted_content


@pytest.mark.asyncio
async def test_two_sessions_have_isolated_state(monkeypatch):
    _posture(monkeypatch, "1")
    be = _FakeBackend()
    for _ in range(4):
        be.push(ExecutionResult(
            stdout=f"\n{STATE_SENTINEL}\x1e__CWD__\x1e/workspace/a\n\x1e__ENV__\x1e\n",
            exit_code=0, backend="fake"))
    t = _tool(be)
    await t.shell_run(ShellRunParams(command="cd a"), execution_context=_owner_ctx(session_id="s1"))
    # session s2 starts fresh at /workspace, not s1's /workspace/a
    await t.shell_run(ShellRunParams(command="pwd"), execution_context=_owner_ctx(session_id="s2"))
    assert "cd /workspace/a" not in be.runs[1].code
