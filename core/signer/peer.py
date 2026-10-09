"""Which PROCESS of a client UID may talk to the signer (security review WAL-1).

The socket authenticates a UID (``SO_PEERCRED``). Every process of the agent UID
shares it — the agent's own host children (an MCP stdio server, git, a lazy
build, a shell command) included — so a UID check alone lets any of them ask
for a signature that no agent-side gate ever saw.

Dropping the signer-clients group from those children is not possible from the
agent: ``setgroups()`` (and so ``setpriv --clear-groups``) needs ``CAP_SETGID``
even to SHRINK the group list, and the agent runs with an empty capability set.
So the signer tells the processes apart instead:

    a client is the EARLIEST-STARTED process of its UID in its own cgroup.

Under systemd that is the unit's main process (``polyrob.service``,
``polyrob-gateway.service``): every child it spawns — and every double-forked
grandchild, which stays in the unit's cgroup — starts later and is refused. An
owner CLI run (``sudo -u polyrob-agent polyrob …``) sits in the owner's session
scope, where it is the earliest process of the agent UID, so it still passes.
Root (UID 0) is the owner and is never checked.

The kernel facts it reads are world-readable: ``/proc/<pid>/cgroup``,
``/proc/<pid>/status`` (``Uid:``), ``/proc/<pid>/stat`` (start time) and the
cgroup's ``cgroup.procs``. The signer unit therefore needs ``ProtectProc=default``
(``invisible`` hides the agent's processes from it). When any fact is missing (no
cgroup v2, a hidden /proc, a vanished pid) the answer is ``None`` — "cannot
tell" — and the caller falls back to the UID check with a logged warning.

Modes (``[server] peer_check`` in ``signer.toml``):

* ``uid``          — the UID check only (the pre-WAL-1 behaviour);
* ``warn``         — the default: check, LOG a non-main peer, still serve it;
* ``main_process`` — refuse a peer that is provably not the main process.
"""
import os
from typing import Iterable, Optional

PEER_CHECK_MODES = ("uid", "warn", "main_process")
DEFAULT_PEER_CHECK = "warn"


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _cgroup_of(pid: int, proc_root: str) -> Optional[str]:
    text = _read(f"{proc_root}/{pid}/cgroup")
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            path = line[3:].strip()
            return path or None
    return None   # cgroup v1 only: cannot tell


def _real_uid(pid: int, proc_root: str) -> Optional[int]:
    text = _read(f"{proc_root}/{pid}/status")
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("Uid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _start_time(pid: int, proc_root: str) -> Optional[int]:
    text = _read(f"{proc_root}/{pid}/stat")
    if not text or ")" not in text:
        return None
    # The comm field may hold spaces and parens: split after the LAST ')'.
    fields = text.rsplit(")", 1)[1].split()
    try:
        return int(fields[19])   # field 22 overall = starttime (clock ticks since boot)
    except (IndexError, ValueError):
        return None


def _pids(cgroup_path: str, cgroup_root: str) -> Optional[Iterable[int]]:
    text = _read(os.path.join(cgroup_root, cgroup_path.lstrip("/"), "cgroup.procs"))
    if text is None:
        return None
    out = []
    for tok in text.split():
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return out


def is_main_process(pid: int, uid: int, *, proc_root: str = "/proc",
                    cgroup_root: str = "/sys/fs/cgroup") -> Optional[bool]:
    """True when *pid* is the earliest-started process of *uid* in its cgroup.

    False when an earlier process of the same UID shares the cgroup (it is a
    child, or a child's child). None when the kernel facts cannot be read.
    """
    if pid <= 0:
        return None
    cg = _cgroup_of(pid, proc_root)
    if cg is None:
        return None
    mine = _start_time(pid, proc_root)
    if mine is None:
        return None
    pids = _pids(cg, cgroup_root)
    if pids is None:
        return None
    for other in pids:
        if other == pid or _real_uid(other, proc_root) != uid:
            continue
        started = _start_time(other, proc_root)
        if started is None:
            continue   # vanished between the listing and the read
        if started < mine or (started == mine and other < pid):
            return False
    return True
