"""WS-2/WS-3 real-docker integration: persistent shell state + background lifecycle.

Skipped when the docker CLI isn't on PATH. Proves against a REAL container:
- cwd persists across shell_run calls (the exact gap that blocked goal 8632a4571b36);
- a background job stays alive, its log is readable, and kill tree-kills it.
"""
import asyncio
import logging
import shutil
import uuid

import pytest

import agents.task.constants as c
from tools.shell.tool import ShellTool, ShellRunParams
from tools.shell.process_tool import ProcessTool, ProcessJobParams
from tools.shell.process_registry import ProcessRegistry
from tools.controller.execution_context import ActionExecutionContext

_needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")


def _pin_workdir(monkeypatch, path):
    monkeypatch.setattr(
        "tools.code_exec.backends.docker.DockerBackend._resolve_persistent_workdir",
        lambda self: str(path),
    )


def _shell(monkeypatch, tmp_path, registry):
    _pin_workdir(monkeypatch, tmp_path)
    t = object.__new__(ShellTool)
    t.logger = logging.getLogger("shell-int")
    t._registry = registry
    t._states = {}
    t._lock = asyncio.Lock()
    # rebind the real resolver (object.__new__ skipped __init__)
    t._resolve_executor = ShellTool._resolve_executor.__get__(t, ShellTool)
    return t


def _process(monkeypatch, tmp_path, registry):
    _pin_workdir(monkeypatch, tmp_path)
    t = object.__new__(ProcessTool)
    t.logger = logging.getLogger("proc-int")
    t._registry = registry
    return t


def _ctx(sid):
    return ActionExecutionContext(role="orchestrator", is_sub_agent=False,
                                  user_id="rob", session_id=sid, metadata={"turn_kind": None})


@pytest.fixture(autouse=True)
def _posture(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setenv("CODE_EXEC_MAX_TIMEOUT_SEC", "60")
    c._refreeze_compute_posture_for_tests()
    yield
    c._refreeze_compute_posture_for_tests()


@_needs_docker
@pytest.mark.asyncio
async def test_cwd_persists_across_real_calls(monkeypatch, tmp_path):
    registry = ProcessRegistry()
    shell = _shell(monkeypatch, tmp_path, registry)
    sid = f"int-{uuid.uuid4().hex}"
    ctx = _ctx(sid)
    try:
        r1 = await shell.shell_run(ShellRunParams(command="mkdir -p sub && cd sub && pwd"),
                                   execution_context=ctx)
        assert not r1.error, r1.error
        assert "/workspace/sub" in r1.extracted_content
        r2 = await shell.shell_run(ShellRunParams(command="pwd"), execution_context=ctx)
        assert not r2.error, r2.error
        assert "/workspace/sub" in r2.extracted_content  # cwd carried across calls
    finally:
        from tools.shell.backend_pool import teardown_session
        await teardown_session(sid)


@_needs_docker
@pytest.mark.asyncio
async def test_background_job_lifecycle(monkeypatch, tmp_path):
    registry = ProcessRegistry()
    shell = _shell(monkeypatch, tmp_path, registry)
    proc = _process(monkeypatch, tmp_path, registry)
    sid = f"int-{uuid.uuid4().hex}"
    ctx = _ctx(sid)
    try:
        started = await shell.shell_run(
            ShellRunParams(command="python -m http.server 8137", background=True),
            execution_context=ctx,
        )
        assert not started.error, started.error
        jobs = registry.list(sid)
        assert len(jobs) == 1
        job_id = jobs[0].id

        await asyncio.sleep(1.0)  # let it bind
        poll = await proc.process_poll(ProcessJobParams(job_id=job_id), execution_context=ctx)
        assert "running" in poll.extracted_content

        killed = await proc.process_kill(ProcessJobParams(job_id=job_id), execution_context=ctx)
        assert "killed" in killed.extracted_content.lower()

        await asyncio.sleep(0.5)
        poll2 = await proc.process_poll(ProcessJobParams(job_id=job_id), execution_context=ctx)
        assert "done" in poll2.extracted_content or "killed" in poll2.extracted_content
    finally:
        from tools.shell.backend_pool import teardown_session
        await teardown_session(sid)


@_needs_docker
@pytest.mark.asyncio
async def test_background_lifecycle_as_nonroot_container_user(monkeypatch, tmp_path):
    """Reproduce PROD: the hardened sandbox forces user 65534:65534 (host runs as
    root) and the bind-mounted /workspace is root-owned, so a background job's
    control files MUST NOT live under /workspace (EACCES) — they live in /tmp.
    The original test ran as the invoking uid (owns tmp_path) so it never hit this;
    forcing the non-root user is the exact prod repro of the pidfile='unknown' bug."""
    monkeypatch.setenv("CODE_EXEC_DOCKER_USER", "65534:65534")
    registry = ProcessRegistry()
    shell = _shell(monkeypatch, tmp_path, registry)
    proc = _process(monkeypatch, tmp_path, registry)
    sid = f"int-{uuid.uuid4().hex}"
    ctx = _ctx(sid)
    try:
        # http.server serves the (readable) cwd and writes nothing to /workspace,
        # so it runs fine as nobody; the control files go to the /tmp tmpfs.
        started = await shell.shell_run(
            ShellRunParams(command="python -m http.server 8231", background=True),
            execution_context=ctx,
        )
        assert not started.error, started.error
        job_id = registry.list(sid)[0].id

        await asyncio.sleep(1.0)
        poll = await proc.process_poll(ProcessJobParams(job_id=job_id), execution_context=ctx)
        assert "running" in poll.extracted_content, (
            f"non-root job should be RUNNING, got: {poll.extracted_content}")

        killed = await proc.process_kill(ProcessJobParams(job_id=job_id), execution_context=ctx)
        assert "killed" in killed.extracted_content.lower()
    finally:
        from tools.shell.backend_pool import teardown_session
        await teardown_session(sid)
