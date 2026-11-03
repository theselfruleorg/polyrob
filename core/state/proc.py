"""Process identity for leases (098 S5).

A pid alone is never proof of life: pids are reused. A lease stores
``process_identity()`` — pid + process start time + boot id — and
:func:`is_alive` checks all three. No psutil: ``/proc`` on Linux, ``ps`` on macOS.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProcIdentity:
    pid: int
    start: str
    boot: str

    def encode(self) -> str:
        return f"{self.pid}|{self.start}|{self.boot}"

    @classmethod
    def decode(cls, raw: Optional[str]) -> Optional["ProcIdentity"]:
        try:
            pid, start, boot = str(raw).split("|", 2)
            return cls(int(pid), start, boot)
        except (ValueError, TypeError, AttributeError):
            return None


_BOOT: Optional[str] = None


def boot_id() -> str:
    global _BOOT
    if _BOOT is None:
        _BOOT = _read_boot_id()
    return _BOOT


def _read_boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", "r") as fh:
            return fh.read().strip()
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                                 text=True, timeout=5).stdout
            # "{ sec = 1730000000, usec = 123 } Mon ..."
            sec = out.split("sec =", 1)[1].split(",", 1)[0].strip()
            return f"boot-{sec}"
        except Exception:
            pass
    return "unknown"


def start_time(pid: int) -> Optional[str]:
    """The process start time as an opaque string, or None when there is no such pid."""
    if os.path.isdir("/proc/self"):
        try:
            with open(f"/proc/{int(pid)}/stat", "r") as fh:
                stat = fh.read()
            # field 22 (starttime) — after the ")" that closes the comm field
            return stat.rsplit(")", 1)[1].split()[19]
        except FileNotFoundError:
            return None
        except (OSError, IndexError):
            # ⚠️ Never fall back to `ps` here: its text can never equal the
            # /proc tick count a lease recorded, so a live owner would read dead.
            return None
    try:
        # ⚠️ `lstart` is printed in the reader's locale and timezone: pin both,
        # or a REPL and a launchd unit read different strings for ONE process
        # and a live lease owner reads as dead (098 review).
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True,
                             text=True, timeout=5,
                             env={**os.environ, "LC_ALL": "C", "LANG": "C", "TZ": "UTC"})
        val = out.stdout.strip()
        return val or None
    except Exception:
        return None


_SELF: Optional[ProcIdentity] = None


def process_identity() -> ProcIdentity:
    """This process's identity (cached per pid, so a fork gets its own)."""
    global _SELF
    pid = os.getpid()
    if _SELF is None or _SELF.pid != pid:
        _SELF = ProcIdentity(pid, start_time(pid) or "unknown", boot_id())
    return _SELF


def is_alive(ident: Optional[ProcIdentity]) -> bool:
    """The process named by ``ident`` still runs: same boot, pid alive, same start time."""
    if ident is None:
        return False
    if ident.boot != boot_id():
        return False
    try:
        os.kill(ident.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, owned by another user
    st = start_time(ident.pid)
    return st is not None and (ident.start == "unknown" or st == ident.start)
