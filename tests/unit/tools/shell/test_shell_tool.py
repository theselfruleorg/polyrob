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
    # LIFO landmine (see the docker-test twins + inbox 2026-07-14): this teardown
    # runs BEFORE monkeypatch reverts env, so refreezing first re-snapshots a
    # test's posture and leaks it into every later test in the process. Pop the
    # envs explicitly, THEN refreeze.
    import os as _os
    _os.environ.pop("AGENT_COMPUTE_POSTURE", None)
    _os.environ.pop("POLYROB_LOCAL", None)
    _os.environ.pop("POLYROB_OWNER_USER_ID", None)
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
    # The owner tenant (`core.identity.LocalIdentity.USER_ID`). ⚠️ Was "polyrob"
    # until 2026-09-15: the owner principal's unbound fallback is no longer the
    # instance id, so an instance-id context is a stranger to the posture gate.
    d = dict(role="orchestrator", is_sub_agent=False, user_id="local",
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


# --- Publishing & app-deployment evaluation 2026-09-05 (Wave 1): the 60 s foreground
# default cut every pip/npm install mid-stride (tool_timeout x15 on prod). ------------

@pytest.mark.asyncio
async def test_foreground_default_timeout_fits_an_install(monkeypatch):
    _posture(monkeypatch, "1")
    monkeypatch.delenv("SHELL_MAX_TIMEOUT_SEC", raising=False)
    be = _FakeBackend()
    t = _tool(be)
    await t.shell_run(ShellRunParams(command="make build"), execution_context=_owner_ctx())
    assert be.runs[-1].timeout == 180.0  # 073 W3 (cross-agent parity); was 120


@pytest.mark.asyncio
async def test_foreground_ceiling_is_env_driven(monkeypatch):
    _posture(monkeypatch, "1")
    be = _FakeBackend()
    t = _tool(be)
    monkeypatch.delenv("SHELL_MAX_TIMEOUT_SEC", raising=False)
    await t.shell_run(ShellRunParams(command="make build", timeout=600),
                      execution_context=_owner_ctx())
    assert be.runs[-1].timeout == 600.0
    monkeypatch.setenv("SHELL_MAX_TIMEOUT_SEC", "45")
    await t.shell_run(ShellRunParams(command="make build", timeout=45),
                      execution_context=_owner_ctx())
    assert be.runs[-1].timeout == 45.0


@pytest.mark.asyncio
async def test_timeout_above_the_ceiling_becomes_a_background_job(monkeypatch):
    """073 W3: above the ceiling is a background job (cross-agent parity), not an error/clamp."""
    _posture(monkeypatch, "1")
    monkeypatch.setenv("SHELL_MAX_TIMEOUT_SEC", "45")
    be = _FakeBackend()
    t = _tool(be)
    r = await t.shell_run(ShellRunParams(command="make build", timeout=9999),
                          execution_context=_owner_ctx())
    assert r.error is None
    assert "background job" in r.extracted_content and "above the foreground ceiling" in r.extracted_content
    assert be.detached and not be.runs


@pytest.mark.asyncio
async def test_relative_recursive_delete_follows_the_persisted_cwd(monkeypatch):
    """`rm -rf build` is ordinary below a project folder; after `cd /` (persisted
    from an earlier call) the same words would delete a system tree: refused."""
    _posture(monkeypatch, "1")
    be = _FakeBackend()
    t = _tool(be)
    ctx = _owner_ctx()
    res = await t.shell_run(ShellRunParams(command="rm -rf build"), execution_context=ctx)
    assert not res.error and len(be.runs) == 1
    t._states[("s1", "docker")] = ShellState(cwd="/")
    res = await t.shell_run(ShellRunParams(command="rm -rf etc"), execution_context=ctx)
    assert res.error and "refused" in res.error and len(be.runs) == 1
    res = await t.shell_run(ShellRunParams(command="rm -rf etc", workdir="/workspace"),
                            execution_context=ctx)
    assert not res.error
