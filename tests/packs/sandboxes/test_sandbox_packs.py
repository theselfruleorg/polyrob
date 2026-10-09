"""073 W7: the cloud/CLI sandbox packs (modal, daytona, vercel_sandbox,
singularity) — loaded through the real 067 loader, their backends reached through
the execution-backend seam, each provider driven through a FAKE SDK / CLI (no
network): argv, credentials kept out of the sandbox, persistence, honest errors."""
import sys
import types

import pytest

from tools.code_exec import default_registry, pack_backends
from tools.code_exec.backend import ExecutionBackendError
from tools.code_exec.result import ExecutionRequest
from tools.shell.executor_protocol import is_shell_executor

PACKS = ("daytona", "modal", "singularity", "vercel_sandbox")


@pytest.fixture(autouse=True)
def _data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    import core.runtime_paths as rp
    monkeypatch.setattr(rp, "effective_data_home", lambda: tmp_path / "data")
    for k in ("CODE_EXEC_NETWORK", "CODE_EXEC_MAX_TIMEOUT_SEC"):
        monkeypatch.delenv(k, raising=False)


def _req(code="echo hi", lang="bash", **kw):
    return ExecutionRequest(language=lang, code=code, timeout=20, **kw)


# --- the packs load and contribute -------------------------------------------------

@pytest.mark.parametrize("pack_id", PACKS)
def test_the_pack_is_loaded_and_contributes_its_backend(pack_id):
    from core.packs import state
    rec = state.record(pack_id)
    assert rec is not None and rec.status == state.LOADED, rec and rec.line()
    assert rec.tier == "first-party" and not rec.manifest.tools
    assert pack_id in pack_backends.shell_backend_names()
    assert pack_backends.source_of(pack_id) == f"pack:{pack_id}"
    backend = default_registry.create(pack_id)           # cheap: no SDK import
    assert backend.capabilities["sandbox"] is True
    ex = pack_backends.shell_executor_factory(pack_id)(session_id="s1", workspace_dir="/tmp/w",
                                                       logs_dir="/tmp/l")
    assert is_shell_executor(ex) and ex.kind == pack_id


def test_a_disabled_pack_contributes_nothing(monkeypatch):
    from core.packs import state
    rec = state.record("modal")
    monkeypatch.setattr(state, "loaded", lambda: [r for r in state.records() if r is not rec])
    pack_backends.unregister_source("pack:modal")
    pack_backends._DISCOVERED.discard("modal")
    assert "modal" not in pack_backends.shell_backend_names()
    with pytest.raises(ExecutionBackendError):
        default_registry.create("modal")
    monkeypatch.undo()
    pack_backends._DISCOVERED.discard("modal")
    assert "modal" in pack_backends.shell_backend_names()


@pytest.mark.asyncio
@pytest.mark.parametrize("pack_id,module", [("modal", "modal"), ("daytona", "daytona"),
                                            ("vercel_sandbox", "vercel.sandbox")])
async def test_a_missing_sdk_is_an_honest_error(monkeypatch, pack_id, module):
    for name in (module, "daytona_sdk", "vercel"):
        monkeypatch.setitem(sys.modules, name, None)       # import -> ImportError
    backend = default_registry.create(pack_id)
    res = await backend.run(_req())
    assert res.exit_code == 2 and "pip install 'polyrob[" in res.stderr


# --- modal ------------------------------------------------------------------------------

class _Stream:
    def __init__(self, data=""):
        self.data, self.written = data, b""

    def read(self):
        return self.data

    def write(self, b):
        self.written += b

    def write_eof(self):
        pass

    def drain(self):
        pass


def _fake_modal(log):
    modal = types.ModuleType("modal")

    class Proc:
        def __init__(self, args, kw):
            log.append(("exec", args, kw))
            self.returncode = 0
            self.stdout, self.stderr, self.stdin = _Stream("out"), _Stream(""), _Stream()

        def wait(self):
            pass

    class Sandbox:
        @staticmethod
        def create(*args, **kw):
            log.append(("create", args, kw))
            return Sandbox()

        def exec(self, *args, **kw):
            return Proc(args, kw)

        def snapshot_filesystem(self):
            log.append(("snapshot",))
            return types.SimpleNamespace(object_id="im-123")

        def terminate(self):
            log.append(("terminate",))

    class Image:
        @staticmethod
        def from_registry(ref):
            return ("registry", ref)

        @staticmethod
        def from_id(i):
            return ("id", i)

    class App:
        @staticmethod
        def lookup(name, **kw):
            log.append(("app", name, kw))
            return "app"

    class Client:
        @staticmethod
        def from_credentials(i, s):
            log.append(("client", i))
            return "client"

    modal.Sandbox, modal.Image, modal.App, modal.Client = Sandbox, Image, App, Client
    return modal


@pytest.mark.asyncio
async def test_modal_runs_in_the_sandbox_and_keeps_the_token_out(monkeypatch):
    log = []
    monkeypatch.setitem(sys.modules, "modal", _fake_modal(log))
    monkeypatch.setenv("MODAL_TOKEN_ID", "tid")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "tsecret")
    from polyrob_modal.backend import ModalBackend
    b = ModalBackend()                                     # ephemeral
    res = await b.run(_req("echo hi", env={"FOO": "1", "MODAL_TOKEN_SECRET": "x",
                                           "GITHUB_TOKEN": "y"}))
    assert res.exit_code == 0 and res.stdout == "out"
    create = [e for e in log if e[0] == "create"][0]
    assert create[2]["block_network"] is True and create[2]["client"] == "client"
    run = [e for e in log if e[0] == "exec" and "echo hi" in e[1]][0]
    flat = " ".join(map(str, run[1]))
    assert "FOO=1" in flat and "tsecret" not in flat and "TOKEN" not in flat
    assert run[1][-5:-2] == ("--signal=KILL", "20", "bash") or "timeout" in flat
    assert log[-1] == ("terminate",)                       # ephemeral: released


@pytest.mark.asyncio
async def test_modal_snapshot_resumes_a_session(monkeypatch):
    log = []
    monkeypatch.setitem(sys.modules, "modal", _fake_modal(log))
    monkeypatch.setenv("MODAL_SANDBOX_SNAPSHOT", "1")
    from polyrob_modal.backend import ModalBackend
    b = ModalBackend(session_id="s1", dev_mode=True)
    await b.setup()
    assert [e for e in log if e[0] == "create"][0][2]["image"] == ("registry", "python:3.12-slim")
    assert [e for e in log if e[0] == "create"][0][2]["block_network"] is False  # dev = networked
    await b.teardown()
    assert ("snapshot",) in log and b.load_state() == {"image_id": "im-123"}
    log.clear()
    b2 = ModalBackend(session_id="s1", dev_mode=True)
    await b2.setup()
    assert [e for e in log if e[0] == "create"][0][2]["image"] == ("id", "im-123")


# --- daytona ------------------------------------------------------------------------------

def _fake_daytona(log, state="started"):
    sdk = types.ModuleType("daytona")

    class Resp:
        exit_code, result = 0, "combined"

    class Process:
        def exec(self, cmd, **kw):
            log.append(("exec", cmd, kw))
            return Resp()

    class Sandbox:
        id = "sb-1"

        def __init__(self):
            self.state = state
            self.process = Process()

        def stop(self):
            log.append(("stop",))

        def start(self):
            log.append(("start",))

        def delete(self):
            log.append(("delete",))

    class Daytona:
        def __init__(self, cfg):
            log.append(("client", cfg))

        def create(self, params):
            log.append(("create", params))
            return Sandbox()

        def get(self, sid):
            log.append(("get", sid))
            return Sandbox()

    sdk.Daytona = Daytona
    sdk.DaytonaConfig = lambda **kw: kw
    sdk.CreateSandboxFromSnapshotParams = lambda **kw: ("snapshot", kw)
    sdk.CreateSandboxFromImageParams = lambda **kw: ("image", kw)
    return sdk


@pytest.mark.asyncio
async def test_daytona_needs_its_key_and_never_forwards_it(monkeypatch):
    log = []
    monkeypatch.setitem(sys.modules, "daytona", _fake_daytona(log))
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    from polyrob_daytona.backend import DaytonaBackend
    res = await DaytonaBackend().run(_req())
    assert res.exit_code == 2 and "DAYTONA_API_KEY" in res.stderr
    monkeypatch.setenv("DAYTONA_API_KEY", "dk-secret")
    res = await DaytonaBackend().run(_req("echo hi", env={"A": "b", "DAYTONA_API_KEY": "x"}))
    assert res.exit_code == 0 and res.stdout == "combined"
    assert log[0] == ("client", {"api_key": "dk-secret"})
    create = [e for e in log if e[0] == "create"][0][1]
    assert create[1]["network_block_all"] is True and "polyrob.session" in create[1]["labels"]
    run = [e for e in log if e[0] == "exec" and "echo hi" in e[1]][0]
    assert run[2].get("env") == {"A": "b"} and "dk-secret" not in run[1]
    assert log[-1] == ("delete",)


@pytest.mark.asyncio
async def test_daytona_stop_and_resume(monkeypatch):
    log = []
    monkeypatch.setitem(sys.modules, "daytona", _fake_daytona(log, state="stopped"))
    monkeypatch.setenv("DAYTONA_API_KEY", "k")
    monkeypatch.setenv("DAYTONA_SANDBOX_PERSIST", "1")
    from polyrob_daytona.backend import DaytonaBackend
    b = DaytonaBackend(session_id="s1", dev_mode=True)
    await b.setup()
    await b.teardown()
    assert ("stop",) in log and ("delete",) not in log and b.load_state() == {"sandbox_id": "sb-1"}
    log.clear()
    b2 = DaytonaBackend(session_id="s1", dev_mode=True)
    await b2.setup()
    assert ("get", "sb-1") in log and ("start",) in log
    assert not [e for e in log if e[0] == "create"]


# --- vercel -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vercel_sandbox_runs_and_stops(monkeypatch):
    log = []
    vercel = types.ModuleType("vercel")
    sandbox_mod = types.ModuleType("vercel.sandbox")

    class Done:
        exit_code = 0

        def stdout(self):
            return "v-out"

        def stderr(self):
            return ""

    class Sandbox:
        @staticmethod
        def create(**kw):
            log.append(("create", kw))
            return Sandbox()

        def run_command(self, cmd, args, **kw):
            log.append(("run", cmd, args, kw))
            return Done()

        def stop(self):
            log.append(("stop",))

    sandbox_mod.Sandbox = Sandbox
    vercel.sandbox = sandbox_mod
    monkeypatch.setitem(sys.modules, "vercel", vercel)
    monkeypatch.setitem(sys.modules, "vercel.sandbox", sandbox_mod)
    monkeypatch.setenv("VERCEL_TOKEN", "vt")
    from polyrob_vercel_sandbox.backend import VercelSandboxBackend
    b = VercelSandboxBackend()
    assert b.capabilities["network"] is True                # no per-sandbox deny
    res = await b.run(_req("echo hi", stdin="data"))
    assert res.exit_code == 0 and res.stdout == "v-out"
    assert log[0][1]["token"] == "vt" and log[0][1]["runtime"] == "python3.13"
    run = log[1]
    assert run[1] == "bash" and "printf" in run[2][1] and "vt" not in str(run)
    assert log[-1] == ("stop",)


# --- singularity ----------------------------------------------------------------------

class FakeCli:
    def __init__(self):
        self.calls = []

    async def __call__(self, argv, *, stdin_bytes=None, timeout=None, label=""):
        self.calls.append(argv)
        return 0, "ok", "", False


@pytest.mark.asyncio
async def test_singularity_instance_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv("SINGULARITY_BINARY", "apptainer")
    from polyrob_singularity.backend import SingularityBackend
    cli = FakeCli()
    b = SingularityBackend(session_id="s1", dev_mode=False, workspace_dir=str(tmp_path),
                           runner=cli)
    res = await b.run(_req("echo hi", env={"K": "v", "AWS_SECRET_ACCESS_KEY": "x"}))
    assert res.exit_code == 0 and res.stdout == "ok"
    start, run = cli.calls[0], cli.calls[1]
    assert start[:3] == ["apptainer", "instance", "start"]
    assert "--containall" in start and f"{tmp_path}:/workspace" in start
    assert start[start.index("--network") + 1] == "none"
    assert run[:2] == ["apptainer", "exec"] and any(a.startswith("instance://polyrob-") for a in run)
    assert "K=v" in run and not any("AWS_SECRET" in a for a in run)
    await b.teardown()
    assert cli.calls[-1][:3] == ["apptainer", "instance", "stop"]


@pytest.mark.asyncio
async def test_singularity_ephemeral_and_shell_executor(monkeypatch, tmp_path):
    monkeypatch.setenv("SINGULARITY_BINARY", "apptainer")
    from polyrob_singularity.backend import SingularityBackend, shell_executor
    cli = FakeCli()
    b = SingularityBackend(runner=cli)
    await b.run(_req("print(1)", lang="python"))
    assert cli.calls[0][:2] == ["apptainer", "exec"] and "docker://python:3.12-slim" in cli.calls[0]
    assert cli.calls[0][-3:] == ["python3", "-c", "print(1)"]
    ex = shell_executor(session_id="s2", workspace_dir=str(tmp_path))
    assert ex.kind == "singularity" and ex.default_cwd == "/workspace"
