"""P0 Task 4 — run_code obeys the sandbox gate."""
import logging

import pytest

from tools.code_exec.tool import CodeExecutionTool, RunCodeParams


def _tool():
    t = object.__new__(CodeExecutionTool)
    t.logger = logging.getLogger("codeexec-test")
    t._backend = None
    return t


@pytest.mark.asyncio
async def test_run_code_refused_on_server_local_backend(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("CODE_EXEC_ENABLED", "true")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "local_subprocess")
    res = await _tool().run_code(RunCodeParams(language="python", code="print(1)"))
    assert res.error and "not a sandbox" in res.error


@pytest.mark.asyncio
async def test_run_code_runs_in_local_mode(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    res = await _tool().run_code(RunCodeParams(language="python", code="print('ok')"))
    assert (res.error in (None, "")) and "ok" in res.extracted_content


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["shell", "SHELL", "bash", "sh", " Shell "])
async def test_every_shell_alias_meets_the_command_guard_floor(monkeypatch, lang):
    """EXEC-2: backends run language="shell" as bash -c, so it is classified too."""
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    res = await _tool().run_code(RunCodeParams(language=lang, code="rm -rf /etc"))
    assert res.error and "refused by command guard" in res.error


@pytest.mark.asyncio
async def test_python_code_is_not_classified_as_shell(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    res = await _tool().run_code(RunCodeParams(language="python", code="print('rm -rf /etc')"))
    assert not (res.error or "").startswith("refused by command guard")
