"""098 TS-08: the persistent docker container is a ResourceLifecycle."""
import asyncio

import pytest

from tools.code_exec.backend import ExecutionBackendError
from tools.code_exec.backends import docker as docker_mod
from tools.code_exec.backends.docker import DockerBackend
from tools.code_exec.result import ExecutionRequest


@pytest.fixture(autouse=True)
def _pin(monkeypatch, tmp_path):
    monkeypatch.setattr(DockerBackend, "_resolve_persistent_workdir", lambda self: str(tmp_path))
    monkeypatch.setenv("CODE_EXEC_NETWORK", "none")


class _Fake:
    def __init__(self):
        self.log = []
        self.dead = set()
        self.run_gate = None
        self.run_raises = None

    async def __call__(self, args, *, input=None, timeout=None):
        self.log.append(list(args))
        if args[:2] == ["run", "-d"]:
            if self.run_gate is not None:
                await self.run_gate.wait()
            if self.run_raises is not None:
                raise self.run_raises
            return (0, "cid\n", "")
        if args and args[0] == "exec":
            name = next(a for a in args if str(a).startswith("polyrob-sbx-"))
            if name in self.dead:
                return (1, "", f"Error response from daemon: No such container: {name}")
            return (0, "ok\n", "")
        return (0, "", "")

    def runs(self):
        return [a for a in self.log if a[:2] == ["run", "-d"]]

    def user_execs(self):
        return [a for a in self.log if a and a[0] == "exec"]


def _req(code="print(1)"):
    return ExecutionRequest(language="python", code=code)


def test_dead_container_is_recreated_on_next_ensure_ready():
    async def go():
        f = _Fake()
        b = DockerBackend(docker_runner=f, session_id="s1")
        await b.setup()
        first = b._container
        f.dead.add(first)
        r = await b.run(_req())
        assert r.exit_code != 0 and "restarted" in r.stderr
        assert b._container is None  # degraded, old one torn down
        assert ["rm", "-f", first] in f.log
        r2 = await b.run(_req())
        assert r2.exit_code == 0 and b._container not in (None, first)
        assert len(f.runs()) == 2 and b.generation == 2
    asyncio.run(go())


def test_user_code_not_rerun_after_restart():
    async def go():
        f = _Fake()
        b = DockerBackend(docker_runner=f, session_id="s1")
        await b.setup()
        f.dead.add(b._container)
        await b.run(_req("side_effect()"))
        assert len([a for a in f.user_execs() if a[-1] == "side_effect()"]) == 1
    asyncio.run(go())


def test_run_d_timeout_removes_container_and_egress():
    async def go():
        f = _Fake()
        f.run_raises = docker_mod._DockerExecTimeout("slow pull")
        b = DockerBackend(docker_runner=f, session_id="s1")
        b._egress_network, b._egress_sidecar = "polyrob-egress-x", "polyrob-egp-x"
        with pytest.raises(ExecutionBackendError):
            await b.setup()
        name = f.runs()[0][f.runs()[0].index("--name") + 1]
        assert ["rm", "-f", name] in f.log
        assert ["rm", "-f", "polyrob-egp-x"] in f.log
        assert ["network", "rm", "polyrob-egress-x"] in f.log
        assert b._container is None
    asyncio.run(go())


def test_teardown_waits_for_setup():
    async def go():
        f = _Fake()
        f.run_gate = asyncio.Event()
        b = DockerBackend(docker_runner=f, session_id="s1")
        st = asyncio.create_task(b.setup())
        await asyncio.sleep(0.01)
        td = asyncio.create_task(b.teardown())
        await asyncio.sleep(0.01)
        f.run_gate.set()
        await st
        await td
        name = f.runs()[0][f.runs()[0].index("--name") + 1]
        assert ["rm", "-f", name] in f.log and b._container is None
    asyncio.run(go())


def test_detached_launch_on_dead_container_retries_once_on_a_new_one():
    async def go():
        f = _Fake()
        b = DockerBackend(docker_runner=f, session_id="s1")
        await b.setup()
        f.dead.add(b._container)
        assert await b.exec_detached("sleep 100") == 0
        assert len(f.runs()) == 2
    asyncio.run(go())
