"""Regression: code_exec must work when asyncio subprocess is unavailable.

In the CLI the agent loop runs on an event loop where
``asyncio.create_subprocess_exec`` raises an (empty) ``NotImplementedError`` —
the classic non-main-thread child-watcher limitation. The backend must not depend
on it; it runs the subprocess via a thread executor instead.
"""
import asyncio

import pytest

from tools.code_exec.backends.local_subprocess import LocalSubprocessBackend
from tools.code_exec.result import ExecutionRequest


@pytest.mark.asyncio
async def test_run_works_when_asyncio_subprocess_unavailable(monkeypatch):
    async def _boom(*a, **k):
        raise NotImplementedError()  # empty str(), like the live CLI failure

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    backend = LocalSubprocessBackend()
    result = await backend.run(ExecutionRequest(language="python", code="print('hi')"))
    assert result.exit_code == 0, f"unexpected error: {result.stderr}"
    assert "hi" in result.stdout


@pytest.mark.asyncio
async def test_timeout_still_kills_when_using_thread_executor(monkeypatch):
    async def _boom(*a, **k):
        raise NotImplementedError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    monkeypatch.setenv("CODE_EXEC_MAX_TIMEOUT_SEC", "1")
    backend = LocalSubprocessBackend()
    result = await backend.run(
        ExecutionRequest(language="python", code="import time; time.sleep(10)", timeout=1)
    )
    assert result.timed_out is True
