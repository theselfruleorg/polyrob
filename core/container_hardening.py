"""The ONE docker hardening-flag list (032 lift from tools/code_exec/backends/docker.py).

Shared by the sandbox backend's ephemeral ``docker run --rm``, its persistent
``docker run -d``, and the app-service supervisor's ``docker run -d`` — kept in
exactly one place so the three can never drift apart. Order is stable (existing
tests locate flags via ``.index()``; keep it regardless). Stdlib only: ``core``
may not import ``tools``, and the supervisor runs in its own process.
"""
from typing import List, Optional


def hardening_flags(
    *,
    network: str,
    workdir_host: str,
    install_host: Optional[str] = None,
    pids_limit: int,
    memory_mb: int,
    shm_size_mb: int,
    cpus: float,
    user: str,
    mount_target: str = "/workspace",
    read_only_mount: bool = False,
    workdir: str = "/workspace",
) -> List[str]:
    """PURE: the hardening flags for one container.

    ``install_host`` (sandbox-dev ONLY) binds an additional writable ``/install``.
    ``mount_target``/``read_only_mount``/``workdir`` let the app-service supervisor
    mount the tested-tree snapshot read-only at ``/app`` — the rootfs stays
    ``--read-only`` and every cap/pid/memory/user flag is identical either way.
    With the defaults the list is byte-identical to the pre-lift sandbox argv.
    """
    mount = f"{workdir_host}:{mount_target}" + (":ro" if read_only_mount else "")
    flags = [
        "--network", network,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp",
        "--pids-limit", str(pids_limit),
        "--memory", f"{memory_mb}m",
        "--memory-swap", f"{memory_mb}m",
        "--shm-size", f"{shm_size_mb}m",
        "--cpus", str(cpus),
        "--user", user,
        "-v", mount,
    ]
    if install_host:
        flags += ["-v", f"{install_host}:/install"]
    flags += ["-w", workdir]
    return flags
