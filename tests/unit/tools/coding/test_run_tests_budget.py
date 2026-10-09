"""Coding-agent review B2 + B3 (2026-09-24): a real test suite must be able to
finish, and its verdict (printed LAST) must reach the model.

B2: ``run_tests`` was clamped to the 30 s ``run_code`` default and the
controller killed the ``coding`` action at the 60 s 'default' cap.
B3: the backend kept the HEAD of the output and killed the suite at the byte
cap; the result cap then kept the head again, so a pytest summary never arrived.
"""
import logging
import os

import pytest

from tools.coding.tool import (CodingTool, RunTestsParams, _RUN_TESTS_LOG_DIR,
                               _RUN_TESTS_TAIL_BYTES)


def _tool(root):
    t = object.__new__(CodingTool)
    t.logger = logging.getLogger("coding-test")
    t._root_override = str(root)
    t._backend = None
    return t


def test_ceiling_raises_the_default_cap_but_an_explicit_cap_wins(monkeypatch):
    from tools.code_exec.limits import exec_timeout_cap

    monkeypatch.delenv("CODE_EXEC_MAX_TIMEOUT_SEC", raising=False)
    assert exec_timeout_cap(30.0, None) == 30.0
    assert exec_timeout_cap(30.0, 300.0) == 300.0
    assert exec_timeout_cap(600.0, 300.0) == 600.0  # a ceiling never lowers
    monkeypatch.setenv("CODE_EXEC_MAX_TIMEOUT_SEC", "30")
    assert exec_timeout_cap(30.0, 300.0) == 30.0


def test_local_backend_clamps_to_the_request_ceiling(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_MAX_TIMEOUT_SEC", raising=False)
    from tools.code_exec.backends.local_subprocess import LocalSubprocessBackend

    b = LocalSubprocessBackend()
    assert b._clamp_timeout(None) == 30.0
    assert b._clamp_timeout(None, 300.0) == 300.0
    assert b._clamp_timeout(500, 300.0) == 300.0


def test_controller_gives_the_coding_tool_the_build_budget():
    from agents.task.constants import TimeoutConfig
    from tools.code_exec.limits import dev_exec_max_timeout_sec

    assert TimeoutConfig.get_tool_timeout("coding") > dev_exec_max_timeout_sec()
    assert TimeoutConfig.get_tool_timeout("coding") == TimeoutConfig.get_tool_timeout("code_execution")


@pytest.mark.asyncio
async def test_run_tests_passes_the_ceiling(monkeypatch, tmp_path):
    seen = {}

    class _Spy:
        async def run(self, req):
            seen["req"] = req
            from tools.code_exec.result import ExecutionResult
            return ExecutionResult(stdout="ok", exit_code=0)

    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.delenv("SHELL_MAX_TIMEOUT_SEC", raising=False)
    t = _tool(tmp_path)

    async def _backend(*a, **k):
        return _Spy()
    t._get_code_exec_backend = _backend
    await t.run_tests(RunTestsParams(command="pytest -q"))
    assert seen["req"].ceiling == 600.0  # 073 W3: the shared ceiling default


@pytest.mark.asyncio
async def test_large_failing_output_keeps_the_verdict_and_spills_the_log(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.setenv("CODE_EXEC_MAX_OUTPUT_BYTES", "100000")
    # 200 KB of noise, THEN the verdict, then a failing exit — the old path
    # killed this at 100 KB and never saw the summary.
    cmd = ("python3 -c \"import sys; sys.stdout.write('.' * 200000 + '\\n'); "
           "print('=== 3 failed, 97 passed ===')\"; exit 1")
    res = await _tool(tmp_path).run_tests(RunTestsParams(command=cmd))
    assert res.error and "tests failed (exit 1)" in res.error
    assert "3 failed, 97 passed" in res.error
    assert len(res.error) < _RUN_TESTS_TAIL_BYTES + 500
    logs = os.listdir(tmp_path / _RUN_TESTS_LOG_DIR)
    assert len(logs) == 1
    full = (tmp_path / _RUN_TESTS_LOG_DIR / logs[0]).read_text()
    assert len(full) > 200000 and full.rstrip().endswith("=== 3 failed, 97 passed ===")
    assert f"full log: {_RUN_TESTS_LOG_DIR}/" in res.error


@pytest.mark.asyncio
async def test_an_exit_inside_the_command_still_reports(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    res = await _tool(tmp_path).run_tests(RunTestsParams(command="echo boom; exit 4"))
    assert res.error and "exit 4" in res.error and "boom" in res.error


@pytest.mark.asyncio
async def test_the_log_is_capped_private_and_unique(monkeypatch, tmp_path):
    import stat

    import tools.coding.tool as coding_tool

    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.setattr(coding_tool, "_RUN_TESTS_LOG_MAX_BYTES", 50000)
    cmd = "python3 -c \"import sys; sys.stdout.write('.' * 200000)\""
    await _tool(tmp_path).run_tests(RunTestsParams(command=cmd))
    await _tool(tmp_path).run_tests(RunTestsParams(command="echo again"))
    logs = sorted((tmp_path / _RUN_TESTS_LOG_DIR).iterdir())
    assert len(logs) == 2  # same second, two distinct names
    sizes = sorted(p.stat().st_size for p in logs)
    assert sizes[-1] == 50000
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in logs)


def test_timeout_never_reads_the_log_on_the_host():
    import inspect

    import tools.coding.tool as coding_tool

    # The log path sits in a tree the sandboxed command controls (codex
    # review 2026-09-25: a planted symlink was followed on the host).
    assert "_read_tail" not in inspect.getsource(coding_tool)
