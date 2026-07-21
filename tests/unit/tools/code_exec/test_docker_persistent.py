"""P1-B — persistent per-session Docker container (docker exec, orphan reaping).

Fake-runner unit tests need NO Docker daemon — the injectable ``self._docker`` seam
(``DockerBackend(docker_runner=...)``) stands in for the real CLI. A second block of
daemon-gated integration tests (skipped when the ``docker`` CLI isn't on PATH)
exercises the real thing end-to-end, including re-confirming containment (non-root,
network-denied, read-only rootfs) holds through ``docker exec`` too — mirroring
``test_docker_escape.py``'s ephemeral-path equivalents.

Deviation from the design brief's illustrative fake: persistent ``run()`` embeds the
request's ``code`` as the TRAILING argv element of the ``docker exec`` call (matching
the ephemeral ``_build_run_argv``'s proven no-shell-argv-list convention — see
``tools/code_exec/backends/docker.py::_run_persistent`` and the task's own binding
spec: "run() in persistent mode = docker exec ... <lang-cmd>"), NOT via the runner's
``input=`` kwarg (``input=`` carries ``request.stdin``, a distinct concept — a
program's own runtime stdin, not its source). The fakes below inspect ``args[-1]``
for the code, not ``input``.

Every real container created here uses a unique ``uuid4``-based session_id (so its
name/labels can't collide with another concurrent session's containers on this
shared dev machine — this repo runs many parallel sessions, see AGENTS.md) and is
torn down in a ``finally`` block.
"""
from __future__ import annotations

import shutil
import time
import uuid

import pytest

from tools.code_exec.backends.docker import DockerBackend
from tools.code_exec.result import ExecutionRequest

_needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")


def _pin_workdir(monkeypatch, path: str) -> None:
    """Skip the real pm()/session-workspace lookup (slow discovery + retry logic
    for a synthetic test session_id) and pin a plain directory instead."""
    monkeypatch.setattr(
        "tools.code_exec.backends.docker.DockerBackend._resolve_persistent_workdir",
        lambda self: path,
    )


# --------------------------------------------------------------------------
# Fake docker-CLI runners — no daemon required.
# --------------------------------------------------------------------------

class _FakeDocker:
    """Records every call; simulates a shared in-container filesystem for `exec`."""

    def __init__(self):
        self.log = []
        self._fs = {}

    async def __call__(self, args, *, input=None, timeout=None):
        self.log.append(list(args))
        if args[:2] == ["run", "-d"]:
            return (0, "cid123\n", "")
        if args and args[0] == "exec":
            code = args[-1] if args else ""
            if "write" in code:
                self._fs["f"] = "42"
                return (0, "", "")
            if "read" in code:
                return (0, self._fs.get("f", "MISSING"), "")
            return (0, "ok\n", "")
        if args and args[0] == "rm":
            return (0, "", "")
        if args and args[0] in ("ps", "inspect"):
            return (0, "", "")
        return (0, "", "")


class _ReapFakeDocker:
    """`ps` returns two labeled container ids; `inspect` reports one very old, one
    very fresh. Only the old one should be eligible for removal."""

    def __init__(self, old_id="old1", young_id="young1"):
        self.log = []
        self.old_id = old_id
        self.young_id = young_id

    async def __call__(self, args, *, input=None, timeout=None):
        self.log.append(list(args))
        if args[0] == "ps":
            return (0, f"{self.old_id}\n{self.young_id}\n", "")
        if args[0] == "inspect":
            ids = args[3:]  # ["inspect", "-f", "{{...}}", id1, id2, ...]
            lines = []
            for cid in ids:
                if cid == self.old_id:
                    lines.append("2020-01-01T00:00:00.000000000Z")
                else:
                    lines.append(time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime()))
            return (0, "\n".join(lines), "")
        if args[0] == "rm":
            return (0, "", "")
        return (0, "", "")


# --------------------------------------------------------------------------
# 1. Ephemeral mode is completely unaffected by the new constructor kwargs
# --------------------------------------------------------------------------

def test_ephemeral_backend_has_no_session_id_by_default():
    assert DockerBackend()._session_id is None


@pytest.mark.asyncio
async def test_ephemeral_mode_never_calls_the_injected_runner():
    """`DockerBackend(docker_runner=fake)` with NO session_id must still take the
    P0 ephemeral `docker run --rm` path (direct subprocess), never touching
    `self._docker` — proves persistence is strictly opt-in, not a silent behavior
    swap for any existing caller that happens to pass a runner.
    """
    fake = _FakeDocker()
    b = DockerBackend(docker_runner=fake)
    assert b._session_id is None
    await b.setup()  # ephemeral setup() only checks shutil.which("docker")
    await b.teardown()  # ephemeral teardown() is a no-op
    assert fake.log == []  # the injected fake was never invoked


# --------------------------------------------------------------------------
# 2. Persistent container is created ONCE and state survives across run() calls
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_container_created_once_and_state_persists_across_runs(monkeypatch):
    _pin_workdir(monkeypatch, "/tmp/rob-persistent-test-ws")
    fake = _FakeDocker()
    b = DockerBackend(docker_runner=fake, session_id="s1")

    await b.setup()
    assert fake.log[0][:2] == ["run", "-d"]  # ONE long-lived container
    assert b._container is not None

    await b.run(ExecutionRequest(language="bash", code="write"))
    r = await b.run(ExecutionRequest(language="bash", code="read"))
    assert "42" in r.stdout  # state persisted across calls
    assert sum(1 for a in fake.log if a[:2] == ["run", "-d"]) == 1  # not re-created per call

    await b.teardown()
    assert any(a[0] == "rm" for a in fake.log)  # reaped on teardown
    assert b._container is None


@pytest.mark.asyncio
async def test_run_without_explicit_setup_lazily_creates_the_container(monkeypatch):
    _pin_workdir(monkeypatch, "/tmp/rob-persistent-test-ws")
    fake = _FakeDocker()
    b = DockerBackend(docker_runner=fake, session_id="s2")
    result = await b.run(ExecutionRequest(language="bash", code="echo hi"))
    assert result.exit_code == 0
    assert fake.log[0][:2] == ["run", "-d"]  # setup() ran implicitly before the exec


@pytest.mark.asyncio
async def test_teardown_is_idempotent(monkeypatch):
    _pin_workdir(monkeypatch, "/tmp/rob-persistent-test-ws")
    fake = _FakeDocker()
    b = DockerBackend(docker_runner=fake, session_id="s3")
    await b.setup()

    await b.teardown()
    rm_after_first = sum(1 for a in fake.log if a and a[0] == "rm")
    await b.teardown()  # second call must be a pure no-op
    rm_after_second = sum(1 for a in fake.log if a and a[0] == "rm")

    assert rm_after_first == 1
    assert rm_after_second == 1


# --------------------------------------------------------------------------
# 3. Persistent setup() argv: labels/name/hardening, no --rm
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persistent_setup_argv_has_labels_name_and_hardening_no_rm(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    _pin_workdir(monkeypatch, "/tmp/rob-persistent-test-ws")
    fake = _FakeDocker()
    b = DockerBackend(docker_runner=fake, session_id="my-session")

    await b.setup()
    argv = fake.log[0]

    assert argv[:2] == ["run", "-d"]
    assert "--rm" not in argv
    assert "--label" in argv and "polyrob.sandbox=1" in argv
    assert "polyrob.session=my-session" in argv
    assert "--name" in argv
    name = argv[argv.index("--name") + 1]
    assert name.startswith("polyrob-sbx-")
    assert b._container == name

    for flag, val in [
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--pids-limit", str(b.pids_limit)),
        ("--memory", f"{b.memory_mb}m"),
        ("--memory-swap", f"{b.memory_mb}m"),
        ("--cpus", str(b.cpus)),
        ("--user", b.user),
        ("--network", "none"),
    ]:
        assert argv[argv.index(flag) + 1] == val, flag
    assert "--read-only" in argv
    assert "--tmpfs" in argv and "/tmp" in argv
    assert "-v" in argv and "/tmp/rob-persistent-test-ws:/workspace" in argv
    assert argv[argv.index("-w") + 1] == "/workspace"
    assert argv[-3:] == [b.image, "sleep", "infinity"]


@pytest.mark.asyncio
async def test_hardening_flags_identical_between_ephemeral_and_persistent(monkeypatch):
    """Drift guard for the 'ONE helper both paths call' requirement: the hardening
    flag/value pairs the persistent `run -d` carries must match what the ephemeral
    `docker run --rm` carries for an equivalent network/workdir.
    """
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    _pin_workdir(monkeypatch, "/tmp/shared-ws")
    fake = _FakeDocker()
    persistent = DockerBackend(docker_runner=fake, session_id="drift-check")
    await persistent.setup()
    persistent_argv = fake.log[0]

    ephemeral = DockerBackend()
    ephemeral_argv = ephemeral._build_run_argv(
        ExecutionRequest(language="bash", code="true"), "/tmp/shared-ws"
    )

    hardening_pairs = [
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--pids-limit", str(ephemeral.pids_limit)),
        ("--memory", f"{ephemeral.memory_mb}m"),
        ("--memory-swap", f"{ephemeral.memory_mb}m"),
        ("--cpus", str(ephemeral.cpus)),
        ("--user", ephemeral.user),
    ]
    for flag, val in hardening_pairs:
        assert persistent_argv[persistent_argv.index(flag) + 1] == val
        assert ephemeral_argv[ephemeral_argv.index(flag) + 1] == val
    assert "--read-only" in persistent_argv and "--read-only" in ephemeral_argv
    assert "/tmp/shared-ws:/workspace" in persistent_argv
    assert "/tmp/shared-ws:/workspace" in ephemeral_argv


# --------------------------------------------------------------------------
# 4. run() persistent-mode argv: docker exec, -w /workspace, env scrubbed, code last
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_persistent_execs_with_workdir_and_scrubbed_env(monkeypatch):
    _pin_workdir(monkeypatch, "/tmp/rob-persistent-test-ws")
    fake = _FakeDocker()
    b = DockerBackend(docker_runner=fake, session_id="s4")
    await b.setup()

    await b.run(ExecutionRequest(
        language="python", code="print(1)", env={"FOO": "bar", "MY_TOKEN": "sk"},
    ))

    exec_argv = next(a for a in fake.log if a and a[0] == "exec")
    assert exec_argv[:1] == ["exec"]
    assert "-w" in exec_argv and exec_argv[exec_argv.index("-w") + 1] == "/workspace"
    assert "-e" in exec_argv and "FOO=bar" in exec_argv
    assert "MY_TOKEN=sk" not in exec_argv  # secret-named stripped, matches ephemeral
    assert exec_argv[-4:] == ["python", "-I", "-c", "print(1)"]  # code last, matches ephemeral


@pytest.mark.asyncio
async def test_run_persistent_reports_unsupported_language_without_touching_runner(monkeypatch):
    _pin_workdir(monkeypatch, "/tmp/rob-persistent-test-ws")
    fake = _FakeDocker()
    b = DockerBackend(docker_runner=fake, session_id="s5")
    await b.setup()
    log_len_before = len(fake.log)

    result = await b.run(ExecutionRequest(language="ruby", code="puts 1"))

    assert result.exit_code == 2
    assert "unsupported language" in result.stderr
    assert len(fake.log) == log_len_before  # never reached self._docker for the exec


# --------------------------------------------------------------------------
# 4b. In-container timeout wrapper (P1-B review, Important #1 fix)
#
# A host-side-only timeout is a foot-gun in persistent mode: `docker exec`'s
# in-container process lifetime is independent of the host CLI client, so
# SIGKILLing the client leaves the process running inside the (reused!)
# session container, eating its pid/memory budget and poisoning every later
# call. The fix wraps the in-container command with coreutils `timeout
# --signal=KILL <clamped_sec>` so the CONTAINER itself bounds the process; the
# host-side call keeps its own timeout only as a slightly-looser backstop.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_persistent_wraps_command_with_in_container_timeout(monkeypatch):
    _pin_workdir(monkeypatch, "/tmp/rob-persistent-test-ws")
    fake = _FakeDocker()
    b = DockerBackend(docker_runner=fake, session_id="s-timeout-argv")
    await b.setup()

    await b.run(ExecutionRequest(language="python", code="print(1)", timeout=7))

    exec_argv = next(a for a in fake.log if a and a[0] == "exec")
    assert "timeout" in exec_argv
    i = exec_argv.index("timeout")
    assert exec_argv[i + 1] == "--signal=KILL"
    assert exec_argv[i + 2] == str(b._clamp_timeout(7))
    # the lang cmd still trails the argv, unaffected by the wrapper
    assert exec_argv[-4:] == ["python", "-I", "-c", "print(1)"]


@pytest.mark.asyncio
async def test_run_persistent_in_container_timeout_uses_clamped_seconds(monkeypatch):
    """A request timeout ABOVE CODE_EXEC_MAX_TIMEOUT_SEC must clamp before being
    handed to the in-container `timeout` binary too — matching `_clamp_timeout`,
    not the raw (unbounded) request value."""
    monkeypatch.setenv("CODE_EXEC_MAX_TIMEOUT_SEC", "9")
    _pin_workdir(monkeypatch, "/tmp/rob-persistent-test-ws")
    fake = _FakeDocker()
    b = DockerBackend(docker_runner=fake, session_id="s-timeout-clamp")
    await b.setup()

    await b.run(ExecutionRequest(language="bash", code="echo hi", timeout=999))

    exec_argv = next(a for a in fake.log if a and a[0] == "exec")
    i = exec_argv.index("timeout")
    assert exec_argv[i + 2] == "9.0"


@pytest.mark.asyncio
async def test_run_persistent_maps_timeout_exit_code_137_to_timed_out(monkeypatch):
    """The REAL-WORLD case: coreutils `timeout --signal=KILL <n>` exits 137
    (128+SIGKILL) when it actually fires and kills the child — NOT 124 (124 is
    only what `timeout` reports for the DEFAULT signal, TERM; empirically
    confirmed live against the `python:3.12-slim` image's coreutils). 137 must
    surface as `ExecutionResult.timed_out=True`, not just a plain nonzero exit.
    """
    _pin_workdir(monkeypatch, "/tmp/rob-persistent-test-ws")

    class _TimeoutFiredFakeDocker(_FakeDocker):
        async def __call__(self, args, *, input=None, timeout=None):
            self.log.append(list(args))
            if args[:2] == ["run", "-d"]:
                return (0, "cid123\n", "")
            if args and args[0] == "exec":
                return (137, "", "")  # `timeout --signal=KILL` fired and killed the child
            return (0, "", "")

    fake = _TimeoutFiredFakeDocker()
    b = DockerBackend(docker_runner=fake, session_id="s-137")
    await b.setup()

    result = await b.run(ExecutionRequest(language="bash", code="sleep 1000", timeout=2))

    assert result.timed_out is True
    assert result.exit_code == 137


@pytest.mark.asyncio
async def test_run_persistent_maps_timeout_exit_code_124_to_timed_out_defensively(monkeypatch):
    """124 is kept as a defensive/secondary mapping (the value coreutils
    `timeout` would report with a non-KILL signal policy) even though the
    wrapper this backend builds always passes `--signal=KILL` today (see the
    137 test above for the actual observed code)."""
    _pin_workdir(monkeypatch, "/tmp/rob-persistent-test-ws")

    class _TimeoutFiredFakeDocker(_FakeDocker):
        async def __call__(self, args, *, input=None, timeout=None):
            self.log.append(list(args))
            if args[:2] == ["run", "-d"]:
                return (0, "cid123\n", "")
            if args and args[0] == "exec":
                return (124, "", "")
            return (0, "", "")

    fake = _TimeoutFiredFakeDocker()
    b = DockerBackend(docker_runner=fake, session_id="s-124")
    await b.setup()

    result = await b.run(ExecutionRequest(language="bash", code="sleep 1000", timeout=2))

    assert result.timed_out is True
    assert result.exit_code == 124


@pytest.mark.asyncio
async def test_run_persistent_unrelated_nonzero_exit_is_not_flagged_as_timed_out(monkeypatch):
    """A plain command failure (e.g. exit 1), or a SIGTERM-style 143, must NOT
    be misread as a timeout — only the specific exit codes `timeout` itself
    uses to report firing (124, 137) mean `timed_out=True`."""
    _pin_workdir(monkeypatch, "/tmp/rob-persistent-test-ws")

    class _FailFakeDocker(_FakeDocker):
        def __init__(self, code):
            super().__init__()
            self._code = code

        async def __call__(self, args, *, input=None, timeout=None):
            self.log.append(list(args))
            if args[:2] == ["run", "-d"]:
                return (0, "cid123\n", "")
            if args and args[0] == "exec":
                return (self._code, "", "boom")
            return (0, "", "")

    for code in (1, 143):
        fake = _FailFakeDocker(code)
        b = DockerBackend(docker_runner=fake, session_id=f"s-fail-{code}")
        await b.setup()

        result = await b.run(ExecutionRequest(language="bash", code="exit 1", timeout=5))

        assert result.timed_out is False, f"exit code {code} was wrongly flagged as timed_out"
        assert result.exit_code == code


@pytest.mark.asyncio
async def test_run_persistent_host_backstop_timeout_exceeds_in_container_timeout(monkeypatch):
    """The host-side `self._docker(...)` call's own `timeout=` kwarg must be
    set HIGHER than the in-container clamped timeout (e.g. +5s) — a backstop
    only, so the in-container `timeout` is what actually fires first."""
    _pin_workdir(monkeypatch, "/tmp/rob-persistent-test-ws")
    seen = {}

    class _CapturingFakeDocker(_FakeDocker):
        async def __call__(self, args, *, input=None, timeout=None):
            if args and args[0] == "exec":
                seen["timeout"] = timeout
            return await super().__call__(args, input=input, timeout=timeout)

    fake = _CapturingFakeDocker()
    b = DockerBackend(docker_runner=fake, session_id="s-backstop")
    await b.setup()

    await b.run(ExecutionRequest(language="bash", code="echo hi", timeout=3))

    clamped = b._clamp_timeout(3)
    assert seen["timeout"] is not None and seen["timeout"] > clamped
    assert seen["timeout"] == clamped + 5


# --------------------------------------------------------------------------
# 5. _resolve_persistent_workdir fallback (no real pm() dependency for this branch)
# --------------------------------------------------------------------------

def test_resolve_persistent_workdir_falls_back_to_tempdir_on_path_manager_failure(tmp_path, monkeypatch):
    def _boom():
        raise RuntimeError("no path manager available in this test context")

    monkeypatch.setattr("agents.task.path.pm", _boom)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    b = DockerBackend(session_id="fallback-check")
    workdir = b._resolve_persistent_workdir()

    assert workdir == str(tmp_path / "rob_docker_persistent_fallback-check")
    import os
    assert os.path.isdir(workdir)


# --------------------------------------------------------------------------
# 6. reap_orphans — age-based sweep
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reap_orphans_removes_only_stale_labeled_containers():
    fake = _ReapFakeDocker()
    n = await DockerBackend.reap_orphans(docker_runner=fake, max_age_sec=3600)
    assert n == 1
    rm_targets = [a[-1] for a in fake.log if a and a[0] == "rm"]
    assert rm_targets == ["old1"]
    ps_call = fake.log[0]
    assert ps_call[0] == "ps"
    assert "label=polyrob.sandbox=1" in " ".join(ps_call)


@pytest.mark.asyncio
async def test_reap_orphans_returns_zero_when_no_labeled_containers():
    class _Empty:
        async def __call__(self, args, *, input=None, timeout=None):
            return (0, "", "")

    n = await DockerBackend.reap_orphans(docker_runner=_Empty())
    assert n == 0


@pytest.mark.asyncio
async def test_reap_orphans_swallows_docker_ps_failure():
    class _Boom:
        async def __call__(self, args, *, input=None, timeout=None):
            raise RuntimeError("no daemon")

    n = await DockerBackend.reap_orphans(docker_runner=_Boom())
    assert n == 0


@pytest.mark.asyncio
async def test_reap_orphans_respects_max_age_threshold():
    """The same two containers (one ancient, one fresh) are BOTH kept when
    max_age_sec is set higher than either's age."""
    fake = _ReapFakeDocker()
    n = await DockerBackend.reap_orphans(docker_runner=fake, max_age_sec=10 ** 12)
    assert n == 0
    assert not any(a and a[0] == "rm" for a in fake.log)


# --------------------------------------------------------------------------
# 7. CODE_EXEC_DOCKER_PERSISTENT flag + resolve_backend wiring
# --------------------------------------------------------------------------

def test_persistent_flag_defaults_off(monkeypatch):
    from tools.code_exec import code_exec_docker_persistent_enabled
    monkeypatch.delenv("CODE_EXEC_DOCKER_PERSISTENT", raising=False)
    assert code_exec_docker_persistent_enabled() is False


def test_resolve_backend_stays_ephemeral_when_flag_off(monkeypatch):
    from tools.code_exec import resolve_backend
    monkeypatch.delenv("CODE_EXEC_DOCKER_PERSISTENT", raising=False)
    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    backend = resolve_backend(session_id="some-session")
    assert backend._session_id is None  # flag off => session_id ignored, ephemeral


def test_resolve_backend_goes_persistent_when_flag_on_and_session_id_given(monkeypatch):
    from tools.code_exec import resolve_backend
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    backend = resolve_backend(session_id="some-session")
    assert backend._session_id == "some-session"


def test_resolve_backend_ignores_flag_without_session_id(monkeypatch):
    """Every EXISTING caller today (tool.py, coding/tool.py) calls resolve_backend()
    with no session_id — must resolve exactly as before even with the flag on."""
    from tools.code_exec import resolve_backend
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    backend = resolve_backend()
    assert backend._session_id is None


def test_resolve_backend_respects_explicit_registry_even_with_flag_and_session(monkeypatch):
    """An explicitly-supplied registry must never be silently bypassed."""
    from tools.code_exec import resolve_backend
    from tools.code_exec.backend import ExecutionBackendRegistry
    from tools.code_exec.backends.local_subprocess import LocalSubprocessBackend
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "local_subprocess")
    reg = ExecutionBackendRegistry()
    reg.register("local_subprocess", LocalSubprocessBackend)
    backend = resolve_backend(reg, session_id="some-session")
    assert isinstance(backend, LocalSubprocessBackend)


# --------------------------------------------------------------------------
# 8. Daemon-gated integration tests (real docker; skipped without the CLI)
# --------------------------------------------------------------------------

@_needs_docker
@pytest.mark.asyncio
async def test_real_persistent_state_survives_across_run_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    monkeypatch.delenv("CODE_EXEC_DOCKER_USER", raising=False)
    _pin_workdir(monkeypatch, str(tmp_path))
    session_id = f"live-{uuid.uuid4().hex[:8]}"
    b = DockerBackend(session_id=session_id)
    try:
        await b.setup()
        assert b._container is not None

        w = await b.run(ExecutionRequest(
            language="python", code="open('/workspace/marker.txt','w').write('42')",
            timeout=20,
        ))
        assert w.exit_code == 0, f"stdout={w.stdout!r} stderr={w.stderr!r}"

        r = await b.run(ExecutionRequest(
            language="python", code="print(open('/workspace/marker.txt').read())",
            timeout=20,
        ))
        assert r.exit_code == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "42" in r.stdout, "state did not persist across two run() calls"
        # proves the SAME container served both calls (host file also visible)
        assert (tmp_path / "marker.txt").read_text() == "42"

        hostname_result = await b.run(ExecutionRequest(
            language="bash", code="cat /etc/hostname", timeout=20,
        ))
        import socket
        assert hostname_result.stdout.strip() != socket.gethostname(), (
            "container hostname matched the host — not actually isolated"
        )
    finally:
        await b.teardown()


@_needs_docker
@pytest.mark.asyncio
async def test_real_persistent_host_created_subdir_writable_on_next_exec(tmp_path, monkeypatch):
    """A long-lived session container's workspace isn't static: a HOST-side tool
    (filesystem/coding) can scaffold a new subdirectory INTO it between two
    `run()` calls on the SAME persistent container (e.g. goal step N creates
    `videos/rob-reboot/`, step N+1 runs npm inside it). setup() only fixes the
    tree once; each exec must re-check for newly-appeared root-owned dirs too —
    live prod hit exactly this (`mkdir EACCES` on a dir created after setup())."""
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    monkeypatch.delenv("CODE_EXEC_DOCKER_USER", raising=False)
    _pin_workdir(monkeypatch, str(tmp_path))
    session_id = f"live-{uuid.uuid4().hex[:8]}"
    b = DockerBackend(session_id=session_id)
    try:
        await b.setup()
        # Simulate a host-side tool (running as root, same as this test process)
        # scaffolding a new directory AFTER the container/workspace was set up.
        (tmp_path / "videos" / "rob-reboot").mkdir(parents=True)

        result = await b.run(ExecutionRequest(
            language="python",
            code=(
                "import os; os.mkdir('/workspace/videos/rob-reboot/node_modules'); "
                "print('mkdir-ok')"
            ),
            timeout=20,
        ))
        assert result.exit_code == 0 and "mkdir-ok" in result.stdout, (
            f"expected a host-created subdir to be writable on the next exec: "
            f"exit={result.exit_code} stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    finally:
        await b.teardown()


@_needs_docker
@pytest.mark.asyncio
async def test_real_persistent_timeout_kills_in_container_process_not_just_client(tmp_path, monkeypatch):
    """P1-B review, Important #1 — the core regression test.

    Before the fix, a persistent-mode timeout only SIGKILLed the HOST-side
    `docker exec` client; the busy-loop process kept running INSIDE the
    (reused!) session container, silently eating its pid/memory budget and
    poisoning every later call on that same container.

    Proof of the fix: run a process that never voluntarily exits with a short
    timeout (must report `timed_out=True`), then immediately run a trivial,
    fast command on the SAME backend/container. If the stuck loop were still
    alive, the container's pid/CPU budget would be under real contention from
    a genuinely busy-looping process; the follow-up call must still complete
    fast and cleanly, proving the in-container `timeout --signal=KILL` really
    killed it (not just detached the host CLI from it).
    """
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    monkeypatch.delenv("CODE_EXEC_DOCKER_USER", raising=False)
    _pin_workdir(monkeypatch, str(tmp_path))
    session_id = f"live-timeout-{uuid.uuid4().hex[:8]}"
    b = DockerBackend(session_id=session_id)
    try:
        await b.setup()

        stuck = await b.run(ExecutionRequest(
            language="python", code="while True: pass", timeout=2,
        ))
        assert stuck.timed_out is True, (
            f"expected the busy loop to be reported as timed out: "
            f"exit={stuck.exit_code} stdout={stuck.stdout!r} stderr={stuck.stderr!r}"
        )

        # Give the in-container SIGKILL a brief moment to actually land before
        # the follow-up call — real-world margin, not part of what's asserted.
        import asyncio as _asyncio
        await _asyncio.sleep(0.5)

        ok = await b.run(ExecutionRequest(
            language="python", code="print('ok')", timeout=10,
        ))
        assert ok.exit_code == 0 and "ok" in ok.stdout, (
            f"SAME container appears poisoned by the earlier stuck process: "
            f"exit={ok.exit_code} stdout={ok.stdout!r} stderr={ok.stderr!r}"
        )
        assert ok.timed_out is False
    finally:
        await b.teardown()


@_needs_docker
@pytest.mark.asyncio
async def test_real_persistent_teardown_removes_container(tmp_path, monkeypatch):
    _pin_workdir(monkeypatch, str(tmp_path))
    session_id = f"live-{uuid.uuid4().hex[:8]}"
    b = DockerBackend(session_id=session_id)
    await b.setup()
    cname = b._container

    await b.teardown()

    import subprocess
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name={cname}"],
        capture_output=True, text=True, timeout=20,
    )
    assert out.stdout.strip() == "", "container still present after teardown()"


@_needs_docker
@pytest.mark.asyncio
async def test_real_persistent_containment_non_root_and_network_denied(tmp_path, monkeypatch):
    """The security invariant `test_docker_escape.py` proves for the ephemeral path
    must ALSO hold when code runs via `docker exec` into the persistent container.
    `docker exec` inherits the container's caps/user/network/read-only rootfs, but
    we verify it empirically rather than assume it.
    """
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    monkeypatch.delenv("CODE_EXEC_DOCKER_USER", raising=False)
    _pin_workdir(monkeypatch, str(tmp_path))
    session_id = f"live-{uuid.uuid4().hex[:8]}"
    b = DockerBackend(session_id=session_id)
    try:
        await b.setup()

        uid_result = await b.run(ExecutionRequest(
            language="python", code="import os; print(os.getuid())", timeout=20,
        ))
        assert uid_result.exit_code == 0, f"stdout={uid_result.stdout!r} stderr={uid_result.stderr!r}"
        assert uid_result.stdout.strip() != "0", (
            f"CONTAINMENT FAILURE: sandboxed code ran as root via docker exec! "
            f"stdout={uid_result.stdout!r}"
        )

        net_result = await b.run(ExecutionRequest(
            language="python",
            code="import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)",
            timeout=20,
        ))
        assert net_result.exit_code != 0, (
            f"CONTAINMENT FAILURE: network egress may have succeeded via docker exec! "
            f"exit={net_result.exit_code} stdout={net_result.stdout!r} stderr={net_result.stderr!r}"
        )

        ro_result = await b.run(ExecutionRequest(
            language="bash", code="touch /etc/pwned_$$", timeout=20,
        ))
        assert ro_result.exit_code != 0, (
            f"CONTAINMENT FAILURE: rootfs was writable via docker exec! "
            f"exit={ro_result.exit_code} stdout={ro_result.stdout!r} stderr={ro_result.stderr!r}"
        )
    finally:
        await b.teardown()


@_needs_docker
@pytest.mark.asyncio
async def test_real_ps_and_inspect_output_shapes_are_parseable(tmp_path, monkeypatch):
    """Verifies the REAL `docker ps -aq --filter label=...` / `docker inspect -f
    {{.State.StartedAt}}` output shapes reap_orphans()'s parsing logic expects —
    WITHOUT invoking the actual sweep. A real sweep would remove ANY
    polyrob.sandbox=1-labeled container on this machine, including ones from a
    concurrent session/test run (this repo runs many parallel sessions — see
    AGENTS.md), so this test scopes the check to its OWN uniquely-labeled container
    via `polyrob.session=<this test's unique id>` and removes it only through the
    normal, scoped teardown() path.
    """
    _pin_workdir(monkeypatch, str(tmp_path))
    session_id = f"live-reap-shape-{uuid.uuid4().hex[:8]}"
    b = DockerBackend(session_id=session_id)
    await b.setup()
    cname = b._container
    try:
        from tools.code_exec.backends.docker import _default_docker_runner, _age_from_docker_timestamp

        code, out, _err = await _default_docker_runner(
            ["ps", "-aq", "--filter", f"label=polyrob.session={session_id}"]
        )
        assert code == 0
        found_id = out.strip()
        assert found_id, "expected exactly one container id from the label filter"

        # `ps -aq` prints IDs, not names — cross-check the id resolves back to our
        # container via `inspect`, proving the label filter found the right one.
        name_code, name_out, _name_err = await _default_docker_runner(
            ["inspect", "-f", "{{.Name}}", found_id]
        )
        assert name_code == 0
        assert name_out.strip().lstrip("/") == cname

        icode, iout, _ierr = await _default_docker_runner(
            ["inspect", "-f", "{{.State.StartedAt}}", cname]
        )
        assert icode == 0
        age = _age_from_docker_timestamp(iout.strip(), time.time())
        assert age is not None and age >= 0
    finally:
        await b.teardown()
