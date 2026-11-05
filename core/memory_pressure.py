"""Hold new autonomous work under memory or disk pressure (102 T-1, N-12).

One memory peak stops every session on the box, so new goals and cron jobs wait while
the box is short. A hold is not a failure: the job stays due and no retry is counted.
⚠️ An owner chat turn is never held: only the goal dispatcher and the cron tick call
``current_hold``. An unreadable source allows the run, and status names it.
"""
import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.async_bridge import spawn_retained
from core.event_kinds import RESOURCE_HOLD
from core.event_log import emit

logger = logging.getLogger(__name__)

MEM_AVAILABLE_FLOOR_BYTES = 500 * 1024 * 1024
DISK_FREE_FLOOR_BYTES = 2 * 1024 * 1024 * 1024
#: A small machine sits under a fixed floor all the time and would hold every job for
#: ever, so a floor is never more than this share of the machine's total.
MEM_FLOOR_MAX_SHARE = 10
DISK_FLOOR_MAX_SHARE = 20
#: memory.current at or over 9/10 of a numeric memory.high.
CGROUP_HIGH_NUM, CGROUP_HIGH_DEN = 9, 10
#: A long hold stays visible in a 24 h status window.
HOLD_RENOTE_S = 3600.0
#: A hold this long is a message to the owner, once per hold: a held reminder is silent.
HOLD_NOTICE_AFTER_S = 3600.0

PROC_ROOT = "/proc"
CGROUP_ROOT = "/sys/fs/cgroup"

_MB = 1024 * 1024
_NOTICE_TASKS: set = set()
_last_noted: Dict[str, Tuple[Tuple[str, ...], float]] = {}
_hold_started: Dict[str, float] = {}
_owner_told = False
_owner_notifier: Optional[Callable[[str], Awaitable[Any]]] = None


def set_owner_notifier(notifier: Optional[Callable[[str], Awaitable[Any]]]) -> None:
    """The composition root gives the rail to the owner: this tier cannot import it."""
    global _owner_notifier
    _owner_notifier = notifier


@dataclass(frozen=True)
class Pressure:
    keys: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()
    unread: Tuple[str, ...] = ()
    mem_available_mb: Optional[int] = None
    cgroup_current_mb: Optional[int] = None
    cgroup_high_mb: Optional[int] = None
    disk_free_mb: Optional[int] = None

    @property
    def hold(self) -> bool:
        return bool(self.keys)


def _read_text(path: str, name: str, unread: List[str], *, optional: bool = False) -> Optional[str]:
    """The file text, or None. An ``optional`` file that is absent is a check that does
    not apply; every other failure names the source in ``unread``."""
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            return handle.read()
    except FileNotFoundError:
        if not optional:
            unread.append(name)
    except OSError:
        unread.append(name)
    return None


def _mem_available(proc_root: str, unread: List[str]) -> Tuple[Optional[int], int]:
    """(MemAvailable, the floor for this machine) in bytes."""
    text = _read_text(os.path.join(proc_root, "meminfo"), "MemAvailable", unread)
    if text is None:
        return None, MEM_AVAILABLE_FLOOR_BYTES
    found: Dict[str, int] = {}
    for line in text.splitlines():
        name = line.split(":", 1)[0]
        if name in ("MemAvailable", "MemTotal"):
            fields = line.split()
            if len(fields) > 1 and fields[1].isdigit():
                found[name] = int(fields[1]) * 1024
    floor = MEM_AVAILABLE_FLOOR_BYTES
    if "MemTotal" in found:
        floor = min(floor, found["MemTotal"] // MEM_FLOOR_MAX_SHARE)
    if "MemAvailable" not in found:
        unread.append("MemAvailable")
    return found.get("MemAvailable"), floor


def _cgroup_dir(proc_root: str, cgroup_root: str, unread: List[str]) -> Optional[str]:
    text = _read_text(os.path.join(proc_root, "self", "cgroup"), "cgroup path", unread,
                      optional=True)
    for line in (text or "").splitlines():
        if line.startswith("0::"):
            return os.path.join(cgroup_root, line[3:].strip().lstrip("/"))
    return None


def _inactive_file(cgroup: str, unread: List[str]) -> int:
    """Page cache the kernel can drop at once: without this subtraction a busy disk
    alone reads as pressure. Absent means no subtraction; unreadable is also named."""
    try:
        with open(os.path.join(cgroup, "memory.stat"), encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("inactive_file "):
                    return int(line.split()[1])
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, IndexError) as e:
        logger.debug("memory.stat not read: %s", e)
        unread.append("memory.stat")
    return 0


def _cgroup_use(proc_root: str, cgroup_root: str,
                unread: List[str]) -> Tuple[Optional[int], Optional[int]]:
    """(memory.current, memory.high) in bytes. ``high`` is None when it is ``max`` or
    absent: the check then does not apply, so ``memory.current`` is not read."""
    cgroup = _cgroup_dir(proc_root, cgroup_root, unread)
    if cgroup is None:
        return None, None
    high_text = _read_text(os.path.join(cgroup, "memory.high"), "memory.high", unread,
                           optional=True)
    if high_text is None or high_text.strip() == "max":
        return None, None
    current_text = _read_text(os.path.join(cgroup, "memory.current"), "memory.current", unread)
    high, current = None, None
    try:
        high = int(high_text.strip())
    except ValueError:
        unread.append("memory.high")
    if current_text is not None:
        try:
            current = int(current_text.strip())
        except ValueError:
            unread.append("memory.current")
    if current is not None:
        current -= _inactive_file(cgroup, unread)
    return current, high


def _disk_free(disk_path: str, unread: List[str]) -> Tuple[Optional[int], int]:
    """(free bytes, the floor for this disk)."""
    try:
        usage = shutil.disk_usage(disk_path)
        return int(usage.free), min(DISK_FREE_FLOOR_BYTES, int(usage.total) // DISK_FLOOR_MAX_SHARE)
    except (OSError, ValueError, TypeError):
        unread.append("free disk")
    return None, DISK_FREE_FLOOR_BYTES


def read_pressure(disk_path: str, *, proc_root: Optional[str] = None,
                  cgroup_root: Optional[str] = None) -> Pressure:
    """Read every source once; ``disk_path`` is on the disk to check. Never raises."""
    unread: List[str] = []
    proc = proc_root or PROC_ROOT
    available, mem_floor = _mem_available(proc, unread)
    current, high = _cgroup_use(proc, cgroup_root or CGROUP_ROOT, unread)
    free, disk_floor = _disk_free(disk_path, unread)
    keys: List[str] = []
    reasons: List[str] = []
    if available is not None and available < mem_floor:
        keys.append("mem_available")
        reasons.append(f"only {available // _MB} MB of memory available")
    if (current is not None and high is not None
            and current * CGROUP_HIGH_DEN >= high * CGROUP_HIGH_NUM):
        keys.append("cgroup_high")
        reasons.append(f"the service uses {current // _MB} MB of its {high // _MB} MB memory.high")
    if free is not None and free < disk_floor:
        keys.append("disk_free")
        reasons.append(f"only {free // _MB} MB of disk free")
    return Pressure(
        keys=tuple(keys), reasons=tuple(reasons), unread=tuple(unread),
        mem_available_mb=None if available is None else available // _MB,
        cgroup_current_mb=None if current is None else current // _MB,
        cgroup_high_mb=None if high is None else high // _MB,
        disk_free_mb=None if free is None else free // _MB)


def current_hold(disk_path: str) -> Optional[Pressure]:
    """The pressure that holds new autonomous work, or None. ⚠️ Autonomous starters only.
    It reads files and a mount: call it through ``asyncio.to_thread`` from the loop."""
    global _owner_told
    pressure = read_pressure(disk_path)
    if pressure.hold:
        return pressure
    _hold_started.clear()
    _owner_told = False
    return None


def _maybe_tell_owner(runner: str, pressure: Pressure, waiting: int, now: float) -> None:
    global _owner_told
    started = _hold_started.setdefault(runner, now)
    if _owner_told or _owner_notifier is None or now - started < HOLD_NOTICE_AFTER_S:
        return
    _owner_told = True
    text = (f"New goals and cron jobs have waited for over {int(HOLD_NOTICE_AFTER_S // 60)} minutes: "
            f"{'; '.join(pressure.reasons)}. {waiting} job(s) wait. They start by themselves "
            "when the machine has room.")
    try:
        spawn_retained(_owner_notifier(text), _NOTICE_TASKS)
    except Exception as e:  # a notice must never stop the tick
        logger.warning("owner hold notice not sent: %s", e)


def note_hold(runner: str, pressure: Pressure, *, waiting: int) -> None:
    """Say that ``runner`` held work: one event per change of reason, per runner, and
    one per hour during a long hold. The ticker fires every 60 s."""
    now = time.monotonic()
    _maybe_tell_owner(runner, pressure, waiting, now)
    last = _last_noted.get(runner)
    if last is not None and last[0] == pressure.keys and now - last[1] < HOLD_RENOTE_S:
        logger.debug("%s held by resource pressure: %s", runner, pressure.keys)
        return
    _last_noted[runner] = (pressure.keys, now)
    logger.warning("%s holds %d waiting job(s): %s", runner, waiting, "; ".join(pressure.reasons))
    emit(RESOURCE_HOLD, source="memory_pressure", attrs={
        "runner": runner, "waiting": waiting, "keys": list(pressure.keys),
        "mem_available_mb": pressure.mem_available_mb,
        "cgroup_current_mb": pressure.cgroup_current_mb,
        "cgroup_high_mb": pressure.cgroup_high_mb,
        "disk_free_mb": pressure.disk_free_mb})
