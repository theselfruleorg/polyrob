"""098 TS-11: the persistent kernel is the interpreter inside the backend."""
import asyncio
import sys
import types

import pytest

from tools.code_exec import kernel as K


class _DockerLike:
    """A backend that looks like a persistent docker sandbox; the kernel argv is
    swapped for a local interpreter so no daemon is needed."""

    name = "docker"
    user = None
    _dev_mode = False
    _session_id = "s"

    def __init__(self):
        self._container = "polyrob-sbx-a"
        self.generation = 1
        self.calls = []

    async def _docker(self, args, timeout=None, input=None):
        self.calls.append(list(args))
        return (0, "", "")


@pytest.fixture
def local_argv(monkeypatch):
    seen = []

    def _argv(backend, workdir):
        seen.append(getattr(backend, "_container", None))
        return [sys.executable, "-u", "-I", "-c", K.KERNEL_SOURCE, "100000"], None, None
    monkeypatch.setattr(K, "kernel_argv", _argv)
    return seen


def test_timeout_kills_inner_pid(local_argv):
    async def go():
        b = _DockerLike()
        try:
            await K.run_cell("t1", b, "x = 1", timeout=20, workdir=None)
            pid = K._KERNELS["t1"].inner_pid
            assert isinstance(pid, int)
            r = await K.run_cell("t1", b, "import time; time.sleep(30)", timeout=1, workdir=None)
            assert r.timed_out
            assert ["exec", "polyrob-sbx-a", "kill", "-9", str(pid)] in b.calls
        finally:
            await K.shutdown_kernel("t1")
    asyncio.run(go())


def test_restart_uses_current_container_name(local_argv):
    async def go():
        b = _DockerLike()
        try:
            await K.run_cell("t2", b, "x = 1", timeout=20, workdir=None)
            b._container, b.generation = "polyrob-sbx-b", 2  # sandbox re-created
            r = await K.run_cell("t2", b, "'x' in globals()", timeout=20, workdir=None)
            assert r.restarted and r.stdout.strip() == "False"
            assert local_argv[-1] == "polyrob-sbx-b" and K._KERNELS["t2"].container == "polyrob-sbx-b"
        finally:
            await K.shutdown_kernel("t2")
    asyncio.run(go())


def test_shutdown_during_execute_is_serialised(local_argv):
    async def go():
        b = _DockerLike()
        await K.run_cell("t3", b, "x = 1", timeout=20, workdir=None)
        cell = asyncio.create_task(
            K.run_cell("t3", b, "import time; time.sleep(0.3); 7", timeout=20, workdir=None))
        await asyncio.sleep(0.05)
        await K.shutdown_kernel("t3")
        r = await cell
        assert r.ok and r.stdout.strip() == "7"  # the cell finished before the kill
    asyncio.run(go())
