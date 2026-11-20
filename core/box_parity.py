"""Does THIS server box match the provisioning its config expects? (runtime-sandbox contract)

A box can lack a provisioning step and say nothing: on 2026-11-20 one box ran
without the trusted lazy installer, every lazy install refused, and the
provider fell back for hours while the status named the configured one. This
module names each missing step. It is a CHECK, not a setting: it reads the
config the owner already chose and the box it runs on.

Only on a server shape (a ``polyrob.service`` system unit). Each line is
``ok``/``MISSING``/``WARN``, with the installer that fixes it. Pure over its
probe (``BoxProbe``), so the rule is tested off Linux; ``polyrob doctor``
renders :func:`parity_lines`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from core.env import parse_bool

#: A tmpfs /tmp below this holds no pip extras install (one box: 955 MB, Errno 122).
TMPFS_FLOOR_MB = 2048
SYSTEMD_DIR = "/etc/systemd/system"
SANDBOX_SOCKET = "/run/polyrob-sandbox/docker.sock"
SANDBOX_VIEW = "/var/lib/polyrob-sandbox-view"
#: Where a self-hoster reads how to provision each step (a shipped guide page).
GUIDE = "docs/guide/self-hosting.md"


@dataclass
class BoxProbe:
    """What the checks read. Defaults read the real box."""
    unit_exists: Callable[[str], bool] = lambda name: os.path.exists(os.path.join(SYSTEMD_DIR, name))
    path_exists: Callable[[str], bool] = os.path.exists
    is_mountpoint: Callable[[str], bool] = os.path.ismount
    tmp_fs: Callable[[], Optional[tuple]] = field(default=None)  # (fstype, size_mb) or None
    env: Dict[str, str] = field(default_factory=lambda: dict(os.environ))

    def __post_init__(self):
        if self.tmp_fs is None:
            self.tmp_fs = _tmp_fs


def _tmp_fs() -> Optional[tuple]:
    """``(fstype, size_mb)`` of the mount that holds /tmp (the longest prefix)."""
    try:
        best = ("", "")
        with open("/proc/self/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and _holds(parts[1], "/tmp") \
                        and len(parts[1]) >= len(best[0]):
                    best = (parts[1], parts[2])
        st = os.statvfs("/tmp")
        return best[1], st.f_blocks * st.f_frsize // (1024 * 1024)
    except OSError:
        return None


def _holds(mount_point: str, path: str) -> bool:
    """The mount at *mount_point* holds the absolute *path*: it is *path* itself or a
    parent of it by whole path components (a mount at ``/t`` does not hold ``/tmp``)."""
    mp = mount_point.rstrip("/") or "/"
    return mp == "/" or path == mp or path.startswith(mp + "/")


def _on(env: Dict[str, str], key: str) -> bool:
    return parse_bool(env.get(key), False, strict=True)


def parity_lines(probe: Optional[BoxProbe] = None) -> List[str]:
    """The ``box:`` lines, or ``[]`` when this is not a server box."""
    p = probe or BoxProbe()
    if not p.unit_exists("polyrob.service"):
        return []
    env = p.env
    out: List[str] = []
    lazy = (env.get("LAZY_DEPS_MODE") or "trusted").strip().lower()
    if lazy == "trusted":
        if p.unit_exists("polyrob-deps.path") and p.path_exists("/var/lib/polyrob-deps/requests"):
            out.append("box: trusted lazy installer ok")
        else:
            out.append("box: trusted lazy installer MISSING — every lazy install refuses "
                       f"(install it: {GUIDE})")
    elif lazy == "legacy":
        out.append("box: WARN LAZY_DEPS_MODE=legacy installs into the read-only venv and refuses "
                   "under the unit — use trusted")
    signer = (env.get("WALLET_SIGNER") or "local").strip().lower()
    if signer in ("shadow", "remote"):
        if p.unit_exists("polyrob-signer.service"):
            out.append(f"box: signer ok (WALLET_SIGNER={signer})")
        else:
            out.append(f"box: signer MISSING — WALLET_SIGNER={signer} needs polyrob-signer.service "
                       f"(install it: {GUIDE})")
    docker_host = env.get("DOCKER_HOST") or ""
    if _on(env, "CODE_EXEC_ENABLED") and SANDBOX_SOCKET in docker_host:
        missing = [what for what, ok in (
            ("the daemon socket", p.path_exists(SANDBOX_SOCKET)),
            ("the workspace view", p.is_mountpoint(f"{SANDBOX_VIEW}/sessions")),
            ("the install view", p.is_mountpoint(f"{SANDBOX_VIEW}/sandbox_installs")),
        ) if not ok]
        out.append("box: code-exec sandbox ok" if not missing else
                   "box: code-exec sandbox MISSING " + ", ".join(missing)
                   + f" (install it: {GUIDE})")
    if _on(env, "APP_SERVICE_ENABLED") or (env.get("AGENT_BUILDER_MODE") or "").strip().lower() == "ship":
        out.append("box: apps supervisor ok" if p.unit_exists("polyrob-apps.service") else
                   "box: apps supervisor MISSING — an approved app never runs (polyrob apps install)")
    tmp = p.tmp_fs()
    if tmp and tmp[0] == "tmpfs" and tmp[1] < TMPFS_FLOOR_MB:
        out.append(f"box: WARN /tmp is a {tmp[1]} MB tmpfs (< {TMPFS_FLOOR_MB} MB): big scratch "
                   "must use <data home>/tmp (core.runtime_paths.scratch_root)")
    return out
