"""P0 Task 5 — coding.run_tests obeys the code-exec sandbox gate."""
import logging

import pytest

from tools.coding.tool import CodingTool, RunTestsParams


def _tool(root):
    t = object.__new__(CodingTool)
    t.logger = logging.getLogger("coding-test")
    t._root_override = str(root)
    t._backend = None
    return t


@pytest.mark.asyncio
async def test_run_tests_refused_on_server(monkeypatch, tmp_path):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("CODE_EXEC_ENABLED", raising=False)
    res = await _tool(tmp_path).run_tests(RunTestsParams(command="echo hi"))
    assert res.error and ("disabled" in res.error or "not a sandbox" in res.error)


@pytest.mark.asyncio
async def test_run_tests_runs_local(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    res = await _tool(tmp_path).run_tests(RunTestsParams(command="echo hi"))
    assert (res.error in (None, "")) and "hi" in res.extracted_content


def test_action_description_not_misleading():
    desc = CodingTool.run_tests._description
    assert "sandbox-gated" in desc or "sandboxed code_exec backend" not in desc
