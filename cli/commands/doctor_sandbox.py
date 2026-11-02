"""``polyrob doctor`` lines for the Docker sandbox: can the agent reach the
Docker socket, and, under wallet custody with the seed in a readable env file,
which setup steps are still missing (``core.security.sandbox_setup``).

The socket answer for THIS process is not the agent's answer on a
``systemctl --user`` install: an ssh login started after ``usermod -aG docker``
holds the new group, while the user manager (``user@<uid>.service``) and every
unit it runs keep the groups of their own older login. On Linux the checker
therefore also reads the user manager's ``Groups:`` from ``/proc``.

Registered in ``cli.commands.doctor_host._checkers``. Never raises.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Mapping, Optional, Set, Tuple

_PROC = Path("/proc")


def _user_manager(uid: int, proc: Path = _PROC) -> Optional[Tuple[int, Set[int]]]:
    """``(pid, groups)`` of the ``systemd --user`` manager of *uid*, or None
    (not Linux, no linger/session, or ``/proc`` hidden)."""
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text()
        except OSError:
            continue
        fields = {}
        for line in status.splitlines():
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        if fields.get("Name") != "systemd" or fields.get("PPid") != "1":
            continue
        uids = fields.get("Uid", "").split()
        if not uids or uids[0] != str(uid):
            continue
        groups = {int(g) for g in fields.get("Groups", "").split() if g.isdigit()}
        gids = fields.get("Gid", "").split()
        if gids and gids[0].isdigit():
            groups.add(int(gids[0]))
        return int(entry.name), groups
    return None


def _socket_lines(env: Mapping[str, str], proc: Path = _PROC) -> Tuple[List[str], bool]:
    """``(lines, agent can reach the socket)``."""
    from core.security import sandbox_setup as ss
    path = ss.docker_socket_path(env)
    if path is None:
        return ["docker socket: DOCKER_HOST is not a unix socket (not probed)"], True
    uid, user = ss.current_identity()
    who = f"uid {uid} {user or '?'}" if uid is not None else "this user"
    access = ss.socket_access(path)
    if not access.exists:
        return [f"docker socket: {path} does not exist (no Docker daemon on this host)"], False
    group = access.group or str(access.gid)
    if not access.reachable:
        hint = ss.stale_group_hint(access, uid)
        if hint:
            return [f"docker socket: ! {path} not reachable by {who}: {hint}"], False
        uid_s = str(uid) if uid not in (None, 0) else "<uid>"
        return [f"docker socket: ! {path} not reachable by {who}: not in group {group} "
                f"(the socket's group); as root `usermod -aG {group} {user or '<agent-user>'}`, "
                f"then `systemctl restart user@{uid_s}.service` (a `systemctl --user` install) "
                "or restart the agent's system unit"], False
    lines = [f"docker socket: {path} reachable by {who} (this doctor process)"]
    if uid in (None, 0) or access.gid is None:
        return lines, True
    manager = _user_manager(uid, proc)
    if manager is None:
        return lines, True
    pid, groups = manager
    if access.in_process_groups and access.gid not in groups:
        lines.append(
            f"docker socket: ! the user manager user@{uid}.service (pid {pid}) does not hold "
            f"group {group}: it started before the group was added, so a `systemctl --user` "
            f"unit cannot reach the socket; as root `systemctl restart user@{uid}.service` "
            "(a `systemctl --user restart` keeps the old groups)")
        return lines, False
    return lines, True


def _custody_lines(env: Mapping[str, str], reachable: bool) -> List[str]:
    try:
        from core.security.host_execution import custody_file_gap
        gap = custody_file_gap()
    except Exception as exc:  # noqa: BLE001
        return [f"custody sandbox: unknown ({type(exc).__name__})"]
    if not gap:
        return []
    from core.env import bool_from
    from core.security.sandbox_setup import DOCKER_SANDBOX_ENV, docker_sandbox_steps
    missing = []
    for name, value in DOCKER_SANDBOX_ENV:
        if value == "true":
            ok = bool_from(env, name, False)
        else:
            ok = (env.get(name) or "").strip().lower() == value
        if not ok:
            missing.append(f"{name}={value}")
    if not missing and reachable:
        return ["custody sandbox: host code is refused (a wallet key is in an env file this "
                "user can read); the Docker sandbox is set and its socket is reachable"]
    left = ", ".join(missing) if missing else "the socket access above"
    return [f"custody sandbox: ! {gap}: run_tests, run_code and the host shell refuse on "
            f"the host, and the app deploy gate needs a green run_tests. Not set yet: {left}. "
            f"Steps: {docker_sandbox_steps()}"]


def docker_sandbox_lines(env: dict) -> List[str]:
    lines, reachable = _socket_lines(env)
    return lines + _custody_lines(env, reachable)


__all__ = ["docker_sandbox_lines"]
