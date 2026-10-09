"""Every process the agent spawns carries one marker; the owner's admin CLI reads it.

The command guard (``core/security/command_guard.py``) classes ``polyrob …`` as a
dangerous head, but a command the agent writes can still reach the owner's CLI
indirectly: a script it wrote, a ``conftest.py`` its test run imports, a
``run_code`` cell calling ``os.system``, or a copied binary. Text classification
cannot see through those. The CLI side can: ``build_child_env`` (the ONE child
env seam) and the docker backend stamp :data:`AGENT_CHILD_ENV` into every agent
child, and ``cli/agent_child_gate.py`` refuses the owner-only verbs when it is
present.

The marker survives a copy of the binary (the env goes with the process, not the
file). A child that unsets it is still caught on Linux: ``/proc/<pid>/environ``
is each ancestor's EXEC-TIME env block, so the shell the agent started still
shows the marker even after ``env -u`` / ``os.environ.pop`` in a grandchild.
Elsewhere only the process's own env is read. A process that daemonizes away
from its ancestors, or Python that imports the wallet module directly, is not
covered here — the CLI gate is defence in depth, not the custody boundary.

This is not a configuration flag: nothing reads it as a setting, and setting it
by hand only ever REMOVES authority.
"""
from __future__ import annotations

import os
from typing import Dict, Mapping, MutableMapping, Optional

#: The marker name. Not secret-shaped, so ``build_child_env``'s scrub keeps it.
AGENT_CHILD_ENV = "POLYROB_AGENT_CHILD"
_MARK = b"\0" + AGENT_CHILD_ENV.encode() + b"="
_MAX_DEPTH = 64


def mark_agent_child(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Stamp the marker into a child env (in place; returned for chaining)."""
    env[AGENT_CHILD_ENV] = "1"
    return env


def agent_child_env_flags() -> Dict[str, str]:
    """The marker as a mapping, for a backend that passes env as flags (docker -e)."""
    return {AGENT_CHILD_ENV: "1"}


def _env_marked(environ: Mapping[str, str]) -> bool:
    value = (environ.get(AGENT_CHILD_ENV) or "").strip()
    return bool(value) and value != "0"


def _proc_ppid(proc_root: str, pid: int) -> Optional[int]:
    try:
        with open(os.path.join(proc_root, str(pid), "stat"), "rb") as fh:
            stat = fh.read()
        # "pid (comm) state ppid …" — comm may hold spaces/parens; split after the LAST ')'.
        return int(stat[stat.rindex(b")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _proc_env_marked(proc_root: str, pid: int) -> bool:
    try:
        with open(os.path.join(proc_root, str(pid), "environ"), "rb") as fh:
            block = b"\0" + fh.read()
    except OSError:
        return False  # not ours to read (another UID, a non-dumpable custody process)
    idx = block.find(_MARK)
    if idx < 0:
        return False
    value = block[idx + len(_MARK):].split(b"\0", 1)[0].strip()
    return bool(value) and value != b"0"


def ancestor_marked(*, pid: Optional[int] = None, proc_root: str = "/proc") -> bool:
    """True when this process or any ancestor was STARTED with the marker (Linux /proc)."""
    if not os.path.isdir(proc_root):
        return False
    current = os.getpid() if pid is None else pid
    for _ in range(_MAX_DEPTH):
        if current is None or current <= 1:
            return False
        if _proc_env_marked(proc_root, current):
            return True
        current = _proc_ppid(proc_root, current)
    return False


def is_agent_child(environ: Optional[Mapping[str, str]] = None, *,
                   pid: Optional[int] = None, proc_root: str = "/proc") -> bool:
    """Was this process started, directly or through any ancestor, by the agent?"""
    if _env_marked(os.environ if environ is None else environ):
        return True
    return ancestor_marked(pid=pid, proc_root=proc_root)


__all__ = ["AGENT_CHILD_ENV", "agent_child_env_flags", "ancestor_marked",
           "is_agent_child", "mark_agent_child"]
