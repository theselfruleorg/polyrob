"""WS-4 real-docker end-to-end: the agent HTTP-tests its OWN sandbox server.

The exact capability whose absence blocked goal 8632a4571b36: start a server inside
the sandbox (background), then reach it over real HTTP from the host via the narrow
published-loopback allowlist. Skipped when docker isn't present.
"""
import asyncio
import logging
import shutil
import uuid

import pytest

import agents.task.constants as c
from tools.shell.tool import ShellTool, ShellRunParams
from tools.shell.process_registry import ProcessRegistry
from tools.shell.backend_pool import get_shell_backend, teardown_session
from tools.shell.loopback_allow import clear_loopback_ports
from tools.controller.execution_context import ActionExecutionContext

_needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setenv("CODE_EXEC_MAX_TIMEOUT_SEC", "60")
    monkeypatch.setenv("CODE_EXEC_PUBLISH_PORTS", "8000")
    monkeypatch.setattr(
        "tools.code_exec.backends.docker.DockerBackend._resolve_persistent_workdir",
        lambda self: str(tmp_path),
    )
    c._refreeze_compute_posture_for_tests()
    clear_loopback_ports()
    yield
    # Fixture teardown is LIFO, so this runs BEFORE monkeypatch reverts the env
    # vars it set above — refreezing here first would just re-snapshot "1".
    # Clear the var ourselves first so the refreeze actually restores posture 0,
    # instead of leaking posture=1 into every later test in this pytest process
    # (the order-dependent tests/unit/tools/ flake on
    # test_child_goal_no_inheritable_parent_tools_falls_back).
    monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
    c._refreeze_compute_posture_for_tests()
    clear_loopback_ports()


def _shell(registry):
    t = object.__new__(ShellTool)
    t.logger = logging.getLogger("ws4-e2e")
    t._registry = registry
    t._states = {}
    t._lock = asyncio.Lock()
    t._resolve_executor = ShellTool._resolve_executor.__get__(t, ShellTool)
    t._loopback_note = ShellTool._loopback_note.__get__(t, ShellTool)
    return t


def _ctx(sid):
    return ActionExecutionContext(role="orchestrator", is_sub_agent=False,
                                  user_id="rob", session_id=sid, metadata={"turn_kind": None})


@_needs_docker
@pytest.mark.asyncio
async def test_agent_http_tests_its_own_sandbox_server(tmp_path):
    registry = ProcessRegistry()
    shell = _shell(registry)
    sid = f"ws4-{uuid.uuid4().hex}"
    ctx = _ctx(sid)
    try:
        # write an index the server will serve, then start http.server in background
        await shell.shell_run(
            ShellRunParams(command="printf '<html>WS4-OK</html>' > index.html"),
            execution_context=ctx)
        started = await shell.shell_run(
            ShellRunParams(command="python -m http.server 8000", background=True),
            execution_context=ctx)
        assert not started.error, started.error

        # the pool registered the published host port into the loopback allowlist
        from tools.shell.backend_pool import peek_backend
        ports = await peek_backend(sid).published_ports()
        assert 8000 in ports, f"port 8000 not published: {ports}"
        host_port = ports[8000]

        # fetch the server over real HTTP via the narrow loopback allowlist
        from tools.web_fetch.fetcher import safe_fetch
        body = None
        for _ in range(10):
            await asyncio.sleep(0.5)
            try:
                res = await safe_fetch(f"http://127.0.0.1:{host_port}/", timeout_sec=5)
                if res.status == 200:
                    body = res.body
                    break
            except Exception:
                continue
        assert body is not None and b"WS4-OK" in body, "server not reachable over loopback"
    finally:
        await teardown_session(sid)
