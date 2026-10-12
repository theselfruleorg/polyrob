"""The mechanics behind ``polyrob service start|stop|restart|logs`` (077).

* **The user bus.** ``systemctl --user`` needs ``XDG_RUNTIME_DIR``. A login
  session sets it; ``sudo -u polyrob …`` and ``su`` do not, and systemctl then
  fails with "Failed to connect to user scope bus". With linger the bus is at
  ``/run/user/<uid>/bus`` whether or not anybody is logged in, so
  :func:`user_bus_env` fills the two variables in when they are missing.
* **Root.** The documented install is a user unit of an unprivileged user. Root
  cannot drive another user's bus directly, so :func:`delegate_as_service_user`
  finds the user that owns the ``polyrob.service`` user unit and runs the SAME
  verb as that user, through the interpreter named in the unit's ``ExecStart``
  (never root's own ``polyrob``), with the unit's ``POLYROB_HOME`` /
  ``POLYROB_PROFILE``.
* **Waiting for the come-back.** :func:`wait_for_request` follows a lifecycle
  request (``core.lifecycle``) through draining → exiting → the history line
  the restarted process writes when it is healthy.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

UNIT_NAME = "polyrob.service"

#: How long a request may stay unclaimed before the CLI concludes the running
#: process cannot act on it (code older than 077) and restarts it directly.
CLAIM_TIMEOUT_SEC = 20.0


def user_bus_env(base: Optional[Dict[str, str]] = None, *,
                 uid: Optional[int] = None,
                 runtime_root: str = "/run/user") -> Dict[str, str]:
    """``base`` (default: this environment) with the user-bus variables filled in
    when they are missing and the lingering runtime dir exists."""
    env = dict(os.environ if base is None else base)
    uid = os.getuid() if uid is None else uid
    runtime = Path(runtime_root) / str(uid)
    if not env.get("XDG_RUNTIME_DIR") and runtime.is_dir():
        env["XDG_RUNTIME_DIR"] = str(runtime)
    rt = env.get("XDG_RUNTIME_DIR")
    if rt and not env.get("DBUS_SESSION_BUS_ADDRESS") and (Path(rt) / "bus").exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={rt}/bus"
    return env


def systemctl_user(*args: str, runner=subprocess.run):
    return runner(["systemctl", "--user", *args], capture_output=True, text=True,
                  env=user_bus_env())


def bus_error_hint(stderr: str) -> str:
    """The remedy for the cross-user bus failure, or ''."""
    if "user scope bus" in (stderr or "") or "XDG_RUNTIME_DIR" in (stderr or ""):
        return ("\n  This shell has no user bus. Run it as the service's user with a "
                "lingering session (`loginctl enable-linger <user>`), or as root: "
                "`polyrob service <verb>` finds that user itself.")
    return ""


# --------------------------------------------------------------------------
# Root → the service user
# --------------------------------------------------------------------------

def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid) and geteuid() == 0


def parse_unit(text: str) -> Tuple[List[str], Dict[str, str]]:
    """``(ExecStart argv, Environment= pairs)`` of a unit file's text."""
    argv: List[str] = []
    env: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("ExecStart="):
            try:
                argv = shlex.split(line[len("ExecStart="):])
            except ValueError:
                argv = line[len("ExecStart="):].split()
        elif line.startswith("Environment="):
            body = line[len("Environment="):]
            try:
                parts = shlex.split(body)
            except ValueError:
                parts = [body.strip('"')]
            for part in parts:
                k, sep, v = part.partition("=")
                if sep:
                    env[k] = v
    return argv, env


def service_users(*, pw_entries=None) -> List[Tuple[str, int, Path]]:
    """``(name, uid, unit path)`` for every account with a polyrob user unit."""
    if pw_entries is None:
        try:
            import pwd
            pw_entries = [(p.pw_name, p.pw_uid, p.pw_dir) for p in pwd.getpwall()]
        except Exception:
            return []
    out = []
    for name, uid, home in pw_entries:
        unit = Path(home) / ".config" / "systemd" / "user" / UNIT_NAME
        try:
            if unit.is_file():
                out.append((name, int(uid), unit))
        except OSError:
            continue
    return out


def delegation_argv(name: str, uid: int, unit: Path, verb_args: List[str],
                    *, have_runuser: bool = True) -> List[str]:
    """The command root runs to act as the service user (pure, testable)."""
    argv, env = parse_unit(unit.read_text(encoding="utf-8"))
    interp = argv[0] if argv else "polyrob"
    home = str(unit.parents[3])
    envs = [f"HOME={home}", f"XDG_RUNTIME_DIR=/run/user/{uid}"]
    for key in ("POLYROB_HOME", "POLYROB_PROFILE"):
        if env.get(key):
            envs.append(f"{key}={env[key]}")
    if Path(interp).name.startswith("python"):
        cmd = [interp, "-I", "-m", "cli.polyrob"]
    else:
        cmd = [interp]
    prefix = ["runuser", "-u", name, "--"] if have_runuser else ["sudo", "-u", name, "--"]
    return [*prefix, "env", *envs, *cmd, "service", *verb_args]


def verb_args_from_argv(argv: Optional[List[str]] = None) -> List[str]:
    """The words after ``service`` on this command line, minus ``--user NAME``."""
    argv = list(sys.argv if argv is None else argv)
    try:
        rest = argv[argv.index("service") + 1:]
    except ValueError:
        return []
    out, skip = [], False
    for word in rest:
        if skip:
            skip = False
            continue
        if word == "--user":
            skip = True
            continue
        if word.startswith("--user="):
            continue
        out.append(word)
    return out


def delegate_as_service_user(user: Optional[str], *, echo: Callable[[str], None],
                             fail: Callable[[str], None],
                             unit_path: Optional[Path] = None) -> Optional[int]:
    """As root with no unit of root's own: run this verb as the service user.

    Returns the delegated command's exit code, or None when no delegation is
    needed (not root, or root has its own unit and no ``--user``)."""
    if not _is_root() or sys.platform != "linux":
        return None
    if not user and unit_path is not None and unit_path.is_file():
        return None
    users = service_users()
    if user:
        users = [u for u in users if u[0] == user]
        if not users:
            fail(f"no {UNIT_NAME} user unit found for user {user!r} "
                 f"(looked under ~{user}/.config/systemd/user/)")
            return 1
    if not users:
        return None
    if len(users) > 1:
        fail("several users run a polyrob service: "
             + ", ".join(u[0] for u in users) + " — pick one with --user NAME")
        return 1
    name, uid, unit = users[0]
    import shutil
    cmd = delegation_argv(name, uid, unit, verb_args_from_argv(),
                          have_runuser=bool(shutil.which("runuser")))
    echo(f"(as {name}, the user that runs {UNIT_NAME})")
    return subprocess.call(cmd)


# --------------------------------------------------------------------------
# Waiting for a request
# --------------------------------------------------------------------------

def wait_for_request(request_id: str, *, data_home, timeout: float,
                     echo: Optional[Callable[[str], None]] = None,
                     poll: float = 1.0,
                     claim_timeout: float = CLAIM_TIMEOUT_SEC,
                     clock: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], None] = time.sleep) -> Optional[dict]:
    """Follow a lifecycle request to its history line.

    Returns that line, ``{"outcome": "unclaimed"}`` when no process took the
    request within ``claim_timeout`` (the request is then withdrawn), or None on
    timeout."""
    from core import lifecycle as lc
    start = clock()
    last_state = None
    while clock() - start < timeout:
        try:
            for entry in reversed(lc.history(limit=50, data_home=data_home)):
                if (entry.get("request") or {}).get("id") == request_id:
                    return entry
            req = lc.read_request(data_home=data_home)
        except lc.LifecycleUnreadable as exc:
            if echo:
                echo(f"  … lifecycle record unreadable: {exc}")
            req = None
        if req is not None and req.id == request_id:
            if req.state != last_state and echo:
                echo({lc.STATE_OPEN: "  … asked the running agent",
                      lc.STATE_DRAINING: "  … the agent is finishing its current work",
                      lc.STATE_EXITING: "  … restarting"}.get(req.state, f"  … {req.state}"))
            last_state = req.state
            if req.state == lc.STATE_OPEN and clock() - start >= claim_timeout:
                if lc.withdraw_if_open(request_id, "no running process took the request",
                                       data_home=data_home):
                    return {"outcome": "unclaimed"}
        sleep(poll)
    return None


def wait_for_healthy_boot(*, data_home, after: float, timeout: float,
                          clock: Callable[[], float] = time.monotonic,
                          sleep: Callable[[float], None] = time.sleep) -> Optional[dict]:
    """The boot record of a process started after ``after`` once it is healthy."""
    from core import lifecycle as lc
    start = clock()
    while clock() - start < timeout:
        try:
            boot = lc.read_boot(data_home=data_home)
        except lc.LifecycleUnreadable:
            boot = None
        if boot and float(boot.get("started_at") or 0) >= after and boot.get("healthy_at"):
            return boot
        sleep(1.0)
    return None


__all__ = ["user_bus_env", "systemctl_user", "bus_error_hint", "parse_unit",
           "service_users", "delegation_argv", "verb_args_from_argv",
           "delegate_as_service_user", "wait_for_request", "wait_for_healthy_boot"]
