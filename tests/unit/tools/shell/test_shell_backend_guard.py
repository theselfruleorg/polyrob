"""CRITICAL guard: the shell tool must run ONLY inside a persistent SANDBOX backend.

Live-review finding: the shell path chose the backend purely by CODE_EXEC_BACKEND
(default `local_subprocess`). With a non-docker backend, `run_foreground` called
`LocalSubprocessBackend.run(...)` — executing the model's command on the HOST (root,
on the prod systemd unit) with no container. `get_shell_backend` must fail-closed
unless the resolved backend is a persistent sandbox (capabilities.sandbox is True AND
it supports the detached/persistent contract), so a misconfig can't silently become
host command execution.
"""
import asyncio
import logging

import pytest

import agents.task.constants as c
from tools.shell.tool import ShellTool, ShellRunParams
from tools.shell.process_registry import ProcessRegistry
from tools.controller.execution_context import ActionExecutionContext


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "CODE_EXEC_BACKEND", "CODE_EXEC_DOCKER_PERSISTENT",
              "POLYROB_OWNER_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    c._refreeze_compute_posture_for_tests()
    yield
    # LIFO landmine (see the docker-test twins + inbox 2026-07-14): this teardown
    # runs BEFORE monkeypatch reverts env, so refreezing first re-snapshots a
    # test's posture and leaks it into every later test in the process. Pop the
    # envs explicitly, THEN refreeze.
    import os as _os
    _os.environ.pop("AGENT_COMPUTE_POSTURE", None)
    _os.environ.pop("CODE_EXEC_BACKEND", None)
    _os.environ.pop("CODE_EXEC_DOCKER_PERSISTENT", None)
    _os.environ.pop("POLYROB_OWNER_USER_ID", None)
    c._refreeze_compute_posture_for_tests()


def _owner_ctx(sid="s1"):
    return ActionExecutionContext(role="orchestrator", is_sub_agent=False, user_id="rob",
                                  session_id=sid, metadata={"turn_kind": None})


@pytest.mark.asyncio
async def test_get_shell_backend_refuses_non_sandbox_backend(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "local_subprocess")  # NOT a sandbox
    c._refreeze_compute_posture_for_tests()
    from tools.shell.backend_pool import get_shell_backend
    with pytest.raises(Exception) as ei:
        await get_shell_backend("s-nonsandbox")
    msg = str(ei.value).lower()
    assert "sandbox" in msg or "docker" in msg


@pytest.mark.asyncio
async def test_shell_run_returns_clear_error_on_non_sandbox_backend(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "local_subprocess")
    c._refreeze_compute_posture_for_tests()

    t = object.__new__(ShellTool)
    t.logger = logging.getLogger("shell-guard-test")
    t._registry = ProcessRegistry()
    t._states = {}
    t._lock = asyncio.Lock()
    t._resolve_executor = ShellTool._resolve_executor.__get__(t, ShellTool)

    res = await t.shell_run(ShellRunParams(command="cat /etc/hostname"),
                            execution_context=_owner_ctx("s-guard"))
    # MUST NOT execute on the host — a clear, actionable refusal instead.
    assert res.error is not None
    assert "docker" in res.error.lower() or "sandbox" in res.error.lower()
    # and no phantom background job leaked into the registry
    assert t._registry.list("s-guard") == []
