"""073: run_code(persist=True) — the session's persistent python kernel (real interpreter)."""
import types

import pytest

from tools.code_exec.kernel import KernelUnavailable, kernel_argv, run_cell, shutdown_kernel


def _local():
    return types.SimpleNamespace(name="local_subprocess")


@pytest.mark.asyncio
async def test_state_persists_and_last_expression_echoes(tmp_path):
    try:
        r = await run_cell("k1", _local(), "x = 41\nimport math", timeout=20, workdir=str(tmp_path))
        assert r.ok and r.restarted
        r = await run_cell("k1", _local(), "x + 1", timeout=20, workdir=str(tmp_path))
        assert r.ok and r.stdout.strip() == "42" and not r.restarted
        r = await run_cell("k1", _local(), "print(math.floor(2.5))", timeout=20, workdir=str(tmp_path))
        assert r.stdout.strip() == "2"
    finally:
        await shutdown_kernel("k1")


@pytest.mark.asyncio
async def test_error_keeps_state_and_reports(tmp_path):
    try:
        await run_cell("k2", _local(), "y = 1", timeout=20, workdir=str(tmp_path))
        r = await run_cell("k2", _local(), "1/0", timeout=20, workdir=str(tmp_path))
        assert not r.ok and "ZeroDivisionError" in r.stderr
        r = await run_cell("k2", _local(), "y", timeout=20, workdir=str(tmp_path))
        assert r.ok and r.stdout.strip() == "1"
    finally:
        await shutdown_kernel("k2")


@pytest.mark.asyncio
async def test_timeout_restarts_and_loses_state(tmp_path):
    try:
        await run_cell("k3", _local(), "z = 5", timeout=20, workdir=str(tmp_path))
        r = await run_cell("k3", _local(), "import time; time.sleep(30)", timeout=1, workdir=str(tmp_path))
        assert r.timed_out and "lost" in r.note
        r = await run_cell("k3", _local(), "'z' in globals()", timeout=20, workdir=str(tmp_path))
        assert r.restarted and r.stdout.strip() == "False"
    finally:
        await shutdown_kernel("k3")


@pytest.mark.asyncio
async def test_reset_and_session_isolation(tmp_path):
    try:
        await run_cell("k4", _local(), "a = 1", timeout=20, workdir=str(tmp_path))
        r = await run_cell("k5", _local(), "'a' in globals()", timeout=20, workdir=str(tmp_path))
        assert r.stdout.strip() == "False"
        r = await run_cell("k4", _local(), "'a' in globals()", timeout=20, workdir=str(tmp_path), reset=True)
        assert r.restarted and r.stdout.strip() == "False"
    finally:
        await shutdown_kernel("k4")
        await shutdown_kernel("k5")


def test_unsupported_backends():
    with pytest.raises(KernelUnavailable):
        kernel_argv(types.SimpleNamespace(name="ssh"), None)
    with pytest.raises(KernelUnavailable):  # one-shot docker
        kernel_argv(types.SimpleNamespace(name="docker", _container=None, _session_id=None), None)


def test_docker_argv_runs_inside_the_container():
    b = types.SimpleNamespace(name="docker", _container="polyrob-sbx-1", _session_id="s",
                              user="1000:1000", _dev_mode=True)
    argv, env, cwd = kernel_argv(b, "/tmp")
    assert argv[1:3] == ["exec", "-i"] and "polyrob-sbx-1" in argv and "PYTHONPATH=/install" in argv
    assert "OPENAI_API_KEY" not in env


@pytest.mark.asyncio
async def test_run_code_persist_param_validation(monkeypatch):
    monkeypatch.setattr("tools.code_exec.sandbox_guard.code_exec_execution_blocked_reason", lambda: None)
    from tools.code_exec.tool import CodeExecutionTool, RunCodeParams
    t = object.__new__(CodeExecutionTool)
    r = await t.run_code(RunCodeParams(language="bash", code="ls", persist=True))
    assert r.error
    r = await t.run_code(RunCodeParams(language="python", code="1", reset_kernel=True))
    assert r.error and "persist" in r.error
