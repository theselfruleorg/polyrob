"""The Docker sandbox setup steps and the Docker socket access check.

A wallet seed in an env file the agent's user can read (a local ``polyrob wallet
init``) refuses model-written code on the host (085 W1,
``core.security.host_execution.model_code_host_refusal``). The way out that
works on every install shape, a ``systemctl --user`` install included, is the
Docker sandbox. Field report 2026-10-29: it took these steps, and the last one
is the trap — a group added to a user reaches a process only through a NEW login
session, so ``systemctl --user restart`` keeps the old groups; the user manager
(``user@<uid>.service``) must restart.

One home for the steps (the custody refusal and ``polyrob doctor`` both print
:func:`docker_sandbox_steps`) and for the socket check (``polyrob doctor`` and
``tools.code_exec.sandbox_guard``). Never raises; pure apart from ``os``/``pwd``/
``grp`` reads.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional, Tuple

DEFAULT_DOCKER_SOCK = "/var/run/docker.sock"

#: The env settings the sandbox needs. None is a posture member (a member would
#: need its ``POLYROB_OVERRIDES=`` form; a direct line is ignored) —
#: ``test_docker_sandbox_env_has_no_posture_member`` pins that. No import of
#: ``core.config_policy.members`` here: it closed an import cycle through
#: ``core.security.host_execution`` (layering ratchet).
DOCKER_SANDBOX_ENV: Tuple[Tuple[str, str], ...] = (
    ("CODE_EXEC_ENABLED", "true"),
    ("CODE_EXEC_BACKEND", "docker"),
    ("CODE_EXEC_NETWORK", "proxy"),
)


def docker_sandbox_env_lines() -> List[str]:
    return [f"{name}={value}" for name, value in DOCKER_SANDBOX_ENV]


def current_identity() -> Tuple[Optional[int], Optional[str]]:
    """``(uid, user name)`` of this process; ``None`` for what is not known."""
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return None, None
    uid = getuid()
    try:
        import pwd
        return uid, pwd.getpwuid(uid).pw_name
    except Exception:  # noqa: BLE001
        return uid, None


def docker_sandbox_steps(uid: Optional[int] = None, user: Optional[str] = None,
                         group: str = "docker") -> str:
    """The numbered steps, with the agent's uid and user name filled in when
    known (a root process does not know them: it prints placeholders)."""
    if uid is None and user is None:
        uid, user = current_identity()
    if uid == 0:
        uid, user = None, None
    who = user or "<agent-user>"
    uid_s = str(uid) if uid is not None else "<uid>"
    env = ", ".join(docker_sandbox_env_lines())
    return (f"(1) in the agent's env file set {env}; "
            f"(2) as root: `usermod -aG {group} {who}` (membership in that group gives "
            f"root on this host); "
            f"(3) as root: `systemctl restart user@{uid_s}.service` on a `systemctl --user` "
            f"install (a `systemctl --user restart` keeps the old groups), or restart the "
            f"agent's system unit; "
            f"(4) as {who}: `polyrob doctor` — its docker socket line must say reachable")


@dataclass(frozen=True)
class SocketAccess:
    """What one uid can do with the Docker socket."""
    path: str
    exists: bool
    reachable: bool = False
    gid: Optional[int] = None
    group: Optional[str] = None
    in_process_groups: bool = False   # the socket's gid is in THIS process's groups
    user_in_group: bool = False       # the group database lists the user


def docker_socket_path(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The unix socket the docker CLI uses, or None for a TCP ``DOCKER_HOST``."""
    src = os.environ if env is None else env
    host = (src.get("DOCKER_HOST") or "").strip()
    if not host:
        return DEFAULT_DOCKER_SOCK
    if host.startswith("unix://"):
        return host[len("unix://"):]
    return None


def _user_in_group(user: Optional[str], gid: int) -> bool:
    if not user:
        return False
    try:
        import grp
        import pwd
        if pwd.getpwnam(user).pw_gid == gid:
            return True
        return user in grp.getgrgid(gid).gr_mem
    except Exception:  # noqa: BLE001
        return False


def socket_access(path: str, *, uid: Optional[int] = None, user: Optional[str] = None,
                  groups: Optional[Iterable[int]] = None) -> SocketAccess:
    """The access *uid* with supplementary *groups* has to the socket at *path*.

    Defaults to this process (``os.getuid()``, ``os.getgroups()`` + the effective
    gid). ``reachable`` reads the mode bits (read + write), not ``os.access``, so a
    caller can ask about another process's groups (``/proc/<pid>/status``)."""
    try:
        st = os.stat(path)
    except OSError:
        return SocketAccess(path=path, exists=False)
    if uid is None or user is None:
        cur_uid, cur_user = current_identity()
        uid = cur_uid if uid is None else uid
        user = cur_user if user is None else user
    if groups is None:
        try:
            groups = set(os.getgroups()) | {os.getegid()}
        except (AttributeError, OSError):
            groups = set()
    groups = set(groups)
    gid = st.st_gid
    try:
        import grp
        group = grp.getgrgid(gid).gr_name
    except Exception:  # noqa: BLE001
        group = None
    mode = stat.S_IMODE(st.st_mode)
    rw = stat.S_IRUSR | stat.S_IWUSR
    if uid == 0:
        reachable = True
    elif uid is not None and st.st_uid == uid:
        reachable = mode & rw == rw
    elif gid in groups:
        reachable = mode & (stat.S_IRGRP | stat.S_IWGRP) == (stat.S_IRGRP | stat.S_IWGRP)
    else:
        reachable = mode & (stat.S_IROTH | stat.S_IWOTH) == (stat.S_IROTH | stat.S_IWOTH)
    return SocketAccess(path=path, exists=True, reachable=reachable, gid=gid, group=group,
                        in_process_groups=gid in groups,
                        user_in_group=_user_in_group(user, gid))


def stale_group_hint(access: SocketAccess, uid: Optional[int] = None) -> Optional[str]:
    """The fix when the user IS in the socket's group but the process is not
    (the group was added after the process's login session started), else None."""
    if not access.exists or access.reachable or access.in_process_groups:
        return None
    if not access.user_in_group:
        return None
    if uid is None:
        uid = current_identity()[0]
    uid_s = str(uid) if uid not in (None, 0) else "<uid>"
    return (f"the user is in group {access.group or access.gid}, but this process started "
            "before that and does not hold it (a group added after login reaches only a new "
            f"login session): as root, `systemctl restart user@{uid_s}.service` on a "
            "`systemctl --user` install, or restart the agent's system unit")


__all__ = ["DEFAULT_DOCKER_SOCK", "DOCKER_SANDBOX_ENV", "SocketAccess", "current_identity",
           "docker_sandbox_env_lines", "docker_sandbox_steps", "docker_socket_path",
           "socket_access", "stale_group_hint"]
