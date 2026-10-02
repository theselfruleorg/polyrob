"""066 P0.1 — a custody process is not dumpable.

``/proc/<pid>/environ`` is the process's EXEC-TIME env block. systemd puts
``AGENT_WALLET_MASTER_SEED`` there (``EnvironmentFile=/etc/polyrob/wallet.env``),
and a process running as the same UID/GID — which every agent child is (pip,
the anysite CLI, git, the Playwright driver) — could read it. Popping the var
from ``os.environ`` (P0.2) does not touch that block.

``prctl(PR_SET_DUMPABLE, 0)`` makes ``/proc/<pid>/{environ,mem,maps,…}``
root-owned, so a same-UID child gets ``EPERM``. Side effects, accepted by 066:
no core dumps, and ``py-spy``/``gdb`` as the agent user cannot attach (root
still can). The flag is reset on ``execve`` — a child is dumpable again, which
is what we want: only the key holder is protected.

Linux only, through ``ctypes`` (no new dependency). Anywhere else the call is a
no-op that reports ``unsupported`` — never ``held``.
"""
import ctypes
import sys
import threading
from dataclasses import dataclass
from typing import Optional

PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4

STATE_HELD = "held"
STATE_FAILED = "failed"
STATE_UNSUPPORTED = "unsupported"
STATE_NOT_APPLIED = "not_applied"


@dataclass(frozen=True)
class HardeningResult:
    state: str
    detail: str = ""

    @property
    def held(self) -> bool:
        return self.state == STATE_HELD


_lock = threading.Lock()
_result: Optional[HardeningResult] = None


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _apply() -> HardeningResult:
    if not _is_linux():
        return HardeningResult(STATE_UNSUPPORTED,
                               f"prctl(PR_SET_DUMPABLE) is Linux-only (platform {sys.platform})")
    try:
        libc = _libc()
        rc = libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
        if rc != 0:
            err = ctypes.get_errno()
            return HardeningResult(STATE_FAILED, f"prctl(PR_SET_DUMPABLE, 0) returned {rc} (errno {err})")
        now = libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0)
    except Exception as e:  # a missing libc symbol is a FAILED hardening, never "held"
        return HardeningResult(STATE_FAILED, f"prctl unavailable: {type(e).__name__}: {e}")
    if now != 0:
        return HardeningResult(STATE_FAILED, f"PR_GET_DUMPABLE reads {now} after the set")
    return HardeningResult(STATE_HELD, "PR_SET_DUMPABLE=0 — /proc/<pid>/environ is root-only")


def harden_custody_process() -> HardeningResult:
    """Make THIS process non-dumpable (Linux). Idempotent; returns the result.

    Call it before the wallet config is read. A ``held`` result is final; a
    ``failed`` one is retried on the next call (the cost is one syscall).
    """
    global _result
    with _lock:
        if _result is not None and _result.state in (STATE_HELD, STATE_UNSUPPORTED):
            return _result
        _result = _apply()
        if _result.state == STATE_FAILED:
            import logging
            logging.getLogger(__name__).warning(
                "custody hardening FAILED: %s — a same-UID child can read this "
                "process's /proc/<pid>/environ", _result.detail)
        return _result


def hardening_state() -> HardeningResult:
    """What :func:`harden_custody_process` achieved in this process so far."""
    with _lock:
        return _result or HardeningResult(STATE_NOT_APPLIED, "harden_custody_process() not called")


def custody_reads_closed() -> bool:
    """True when a same-UID child cannot read this process's exec-time env.

    ``held`` on Linux. ``unsupported`` elsewhere counts as closed: there is no
    ``/proc`` there, and a local install loads its seed from a dotenv file
    AFTER exec, so the seed is not in the exec-time block to begin with.
    """
    return harden_custody_process().state in (STATE_HELD, STATE_UNSUPPORTED)


def _reset_for_tests() -> None:
    global _result
    with _lock:
        _result = None
