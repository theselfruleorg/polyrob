"""Docker sandbox escape-attempt tests (P0 sandbox security deliverable).

These are LIVE integration tests against a real Docker daemon (not argv-shape
unit tests — see ``test_docker_backend.py`` for the pure ``_build_run_argv``
coverage). Each test builds a real ``DockerBackend``, runs a short adversarial
snippet INSIDE the hardened container, and asserts the containment property
still holds: non-root execution, network-deny-by-default, read-only rootfs
(with tmpfs ``/tmp`` and the workspace bind mount as the only writable
exceptions), the workspace bind mount actually round-trips to the host, and
the PID cap is wired.

Companion doc: ``tools/code_exec/SANDBOX_SECURITY.md`` (threat model + the
reasoning behind every flag). If a test here starts failing, treat it as a
sandbox regression, not a flaky test — do not silence it.

Tests that touch the real daemon are ``@pytest.mark.skipif(shutil.which("docker")
is None, ...)`` + ``@pytest.mark.asyncio``, and each carries a small, explicit
``timeout=`` on its ``ExecutionRequest`` so a stuck container can't hang the
suite. ``test_pids_limit_present_in_argv`` is deliberately NOT daemon-gated —
it proves ``--pids-limit`` is wired via the pure argv builder, no Docker
required (see the PID-limit section below for why no fork bomb is ever run).

SAFE by construction: no fork bombs, no unbounded resource consumption, no
host mutation outside a pytest ``tmp_path``.
"""
from __future__ import annotations

import shutil

import pytest

from tools.code_exec.backends.docker import DockerBackend
from tools.code_exec.result import ExecutionRequest

# Generous enough for container startup + a trivial snippet, well under the
# backend's own CODE_EXEC_MAX_TIMEOUT_SEC default (30s) clamp.
TIMEOUT = 20.0

_needs_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker not installed"
)


async def _ready_backend(monkeypatch) -> DockerBackend:
    """A DockerBackend under the DEFAULT hardening posture (deterministic env).

    Clears ``CODE_EXEC_NETWORK`` so the default network policy (none) applies,
    and ``CODE_EXEC_DOCKER_USER`` so the default non-root user resolution
    applies, regardless of the ambient shell environment.
    """
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    monkeypatch.delenv("CODE_EXEC_DOCKER_USER", raising=False)
    backend = DockerBackend()
    await backend.setup()
    return backend


# --------------------------------------------------------------------------
# 1. Non-root execution
# --------------------------------------------------------------------------

@_needs_docker
@pytest.mark.asyncio
async def test_container_does_not_run_as_root(monkeypatch):
    """Agent code must never run as uid 0 inside the sandbox container.

    On this (non-root) host the container runs as the host uid; on a root
    host (e.g. prod systemd ``User=root``) ``DockerBackend`` forces
    ``65534:65534`` instead (see ``test_docker_user_forced_unprivileged_when_
    host_is_root`` in ``test_docker_backend.py`` for that branch, argv-only).
    Either way the printed uid must never be "0".
    """
    backend = await _ready_backend(monkeypatch)
    result = await backend.run(ExecutionRequest(
        language="python",
        code="import os; print(os.getuid())",
        timeout=TIMEOUT,
    ))
    assert result.exit_code == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    uid = result.stdout.strip()
    assert uid != "0", (
        f"CONTAINMENT FAILURE: sandboxed code ran as root (uid 0)! "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------
# 2. Network denied by default
# --------------------------------------------------------------------------

@_needs_docker
@pytest.mark.asyncio
async def test_network_egress_denied_by_default(monkeypatch):
    """``--network none`` must block outbound egress even to a trivial well-known IP."""
    backend = await _ready_backend(monkeypatch)
    result = await backend.run(ExecutionRequest(
        language="python",
        code="import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)",
        timeout=TIMEOUT,
    ))
    assert result.exit_code != 0 and result.stderr.strip(), (
        f"CONTAINMENT FAILURE: network egress may have succeeded from the sandbox! "
        f"exit={result.exit_code} stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------
# 3. Read-only rootfs, with tmpfs /tmp and the workspace mount as exceptions
# --------------------------------------------------------------------------

@_needs_docker
@pytest.mark.asyncio
async def test_readonly_rootfs_blocks_write_outside_mounts(monkeypatch, tmp_path):
    """``--read-only`` must block a write to a rootfs path like ``/etc``."""
    backend = await _ready_backend(monkeypatch)
    result = await backend.run(ExecutionRequest(
        language="bash",
        code="touch /etc/pwned_$$",
        timeout=TIMEOUT,
        workdir=str(tmp_path),
    ))
    assert result.exit_code != 0, (
        f"CONTAINMENT FAILURE: rootfs was writable (touch /etc/... succeeded)! "
        f"exit={result.exit_code} stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@_needs_docker
@pytest.mark.asyncio
async def test_tmpfs_tmp_is_writable(monkeypatch, tmp_path):
    """``--tmpfs /tmp`` is the deliberate writable exception to the read-only rootfs."""
    backend = await _ready_backend(monkeypatch)
    result = await backend.run(ExecutionRequest(
        language="bash",
        code="touch /tmp/ok && echo wrote-tmp",
        timeout=TIMEOUT,
        workdir=str(tmp_path),
    ))
    assert result.exit_code == 0 and "wrote-tmp" in result.stdout, (
        f"expected /tmp (tmpfs) to be writable: exit={result.exit_code} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@_needs_docker
@pytest.mark.asyncio
async def test_workspace_bind_mount_is_writable(monkeypatch, tmp_path):
    """The ``-v <workdir>:/workspace`` bind mount is the other writable exception."""
    backend = await _ready_backend(monkeypatch)
    result = await backend.run(ExecutionRequest(
        language="python",
        code="open('/workspace/f.txt', 'w').write('hi'); print('wrote-workspace')",
        timeout=TIMEOUT,
        workdir=str(tmp_path),
    ))
    assert result.exit_code == 0 and "wrote-workspace" in result.stdout, (
        f"expected /workspace (bind mount) to be writable: exit={result.exit_code} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@_needs_docker
@pytest.mark.asyncio
async def test_workspace_nested_preexisting_dir_is_writable(monkeypatch, tmp_path):
    """A HOST-side tool (e.g. the `filesystem` tool's create_directory, running as
    root outside the container) commonly scaffolds nested directories under the
    workspace BEFORE the container ever touches them — e.g. `videos/rob-reboot/`
    for a Remotion project. Chmod'ing only the top-level workspace mount doesn't
    reach those: live prod hit `mkdir EACCES` one level inside such a directory
    (npm trying to create `node_modules`/`out`). The recursive (pruned) chmod
    must reach pre-existing nested dirs too, not just the mount root."""
    nested = tmp_path / "videos" / "rob-reboot"
    nested.mkdir(parents=True)
    backend = await _ready_backend(monkeypatch)
    result = await backend.run(ExecutionRequest(
        language="python",
        code=(
            "import os; os.mkdir('/workspace/videos/rob-reboot/node_modules'); "
            "print('mkdir-ok')"
        ),
        timeout=TIMEOUT,
        workdir=str(tmp_path),
    ))
    assert result.exit_code == 0 and "mkdir-ok" in result.stdout, (
        f"expected a pre-existing nested workspace dir to be writable: "
        f"exit={result.exit_code} stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------
# 4. Workspace confinement: the bind mount round-trips to the host, and only there
# --------------------------------------------------------------------------

@_needs_docker
@pytest.mark.asyncio
async def test_workspace_write_appears_on_host(monkeypatch, tmp_path):
    """A file written to ``/workspace`` inside the container must land in the
    exact host ``request.workdir`` — proving the bind mount is workspace-scoped,
    not a passthrough to some other host path. (A write outside the mount
    failing is covered by ``test_readonly_rootfs_blocks_write_outside_mounts``.)
    """
    backend = await _ready_backend(monkeypatch)
    result = await backend.run(ExecutionRequest(
        language="python",
        code="open('/workspace/marker.txt', 'w').write('marker'); print('done')",
        timeout=TIMEOUT,
        workdir=str(tmp_path),
    ))
    assert result.exit_code == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    marker = tmp_path / "marker.txt"
    assert marker.exists(), (
        "CONTAINMENT FAILURE: /workspace write inside the container did not "
        f"propagate to the host workdir {tmp_path} — bind mount is not workspace-scoped."
    )
    assert marker.read_text() == "marker"


# --------------------------------------------------------------------------
# 5. PID limit is wired (SAFE — no fork bomb is ever executed)
# --------------------------------------------------------------------------

def test_pids_limit_present_in_argv():
    """Pure argv assertion, no Docker daemon required: ``--pids-limit`` is always set.

    This is the required SAFE variant of the PID-bomb containment test — it
    proves the cap is wired into every ``docker run`` invocation without ever
    spawning a process, let alone a fork bomb. Deliberately NOT daemon-gated.
    """
    argv = DockerBackend()._build_run_argv(
        ExecutionRequest(language="bash", code="true"), "/tmp/ws"
    )
    assert "--pids-limit" in argv
    limit = argv[argv.index("--pids-limit") + 1]
    assert int(limit) > 0


@_needs_docker
@pytest.mark.asyncio
async def test_pids_limit_allows_bounded_process_spawn(monkeypatch):
    """A small, FIXED-count, short-lived process spawn must still succeed —
    proving ``--pids-limit`` caps runaway growth without breaking ordinary
    bounded work. This spawns exactly 20 near-instant subshells (well under
    the default 256 limit) and waits for them; it is NOT a fork bomb (no
    self-replication, no unbounded loop, no host resource pressure — every
    spawned process is a single ``true`` invocation that exits immediately).
    """
    backend = await _ready_backend(monkeypatch)
    result = await backend.run(ExecutionRequest(
        language="bash",
        code="for i in $(seq 1 20); do true & done; wait; echo spawned-ok",
        timeout=TIMEOUT,
    ))
    assert result.exit_code == 0 and "spawned-ok" in result.stdout, (
        f"bounded process spawn should stay within pids-limit and succeed: "
        f"exit={result.exit_code} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
