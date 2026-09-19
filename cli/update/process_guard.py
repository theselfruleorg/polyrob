"""Guards for destructive `polyrob update` operations (restore / apply).

``restore_snapshot`` does an ``os.replace`` on every DB file and deletes the
``-wal``/``-shm`` sidecars. Performed *under a live POLYROB process* holding those
DBs open in WAL mode, that can corrupt state or be silently undone by a leftover
WAL. So before any restore we refuse while a process is actively using a DB — unless
the operator passes ``--force``.

Two independent, best-effort signals:

1. **Active SQLite writer.** Probe each DB with ``BEGIN IMMEDIATE`` at
   ``busy_timeout=0``. A ``database is locked``/``busy`` error means another
   connection holds the write lock. (An *idle* agent between steps holds no write
   lock, so this can't prove exclusivity — it reliably catches an active writer.)
2. **Workspace turn lock.** The local CLI holds a cross-process ``workspace.turn.lock``
   for the duration of a turn. If it exists and can't be acquired non-blocking, a
   live REPL/turn is running.

Separately, :func:`update_lock` serialises concurrent update/rollback invocations via
a flock so two ``--rollback`` (or a future apply) can't race each other.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, List, Optional


class UpdateLockHeld(RuntimeError):
    """Raised when another update/rollback process already holds ``update.lock``."""


class ProcessScanUnavailable(RuntimeError):
    """Exclusive access cannot be established when process enumeration fails."""


# Intra-process serialisation: POSIX advisory locks (fcntl) do NOT self-conflict
# within a single process, so the flock alone can't stop a second same-process
# acquisition. This registry makes the guard deterministic in-process; the flock
# extends it across processes.
_HELD: set = set()
_HELD_LOCK = threading.Lock()


def _db_locked(path: Path) -> bool:
    """True if a writer currently holds ``path`` (best-effort, never raises)."""
    try:
        conn = sqlite3.connect(str(path), timeout=0)
    except sqlite3.OperationalError:
        return True  # cannot even open it — treat as in use
    except Exception:
        return False
    try:
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        return "lock" in msg or "busy" in msg
    except Exception:
        return False  # "file is not a database" etc. — not a lock signal
    finally:
        conn.close()


def dbs_in_use(db_paths: Iterable[Path]) -> List[Path]:
    """Subset of ``db_paths`` that exist AND are currently locked by a writer."""
    out: List[Path] = []
    for p in db_paths:
        p = Path(p)
        if p.is_file() and _db_locked(p):
            out.append(p)
    return out


def workspace_lock_busy(lock_dir: Optional[Path] = None) -> bool:
    """True if the cross-process workspace turn lock is currently held.

    Uses the CLI's ``POLYROB_WORKSPACE_LOCK_DIR`` convention when ``lock_dir`` is not
    given. Inert (returns False) when no lock is configured — the SQLite probe is then
    the only signal.
    """
    root = str(lock_dir) if lock_dir else os.environ.get("POLYROB_WORKSPACE_LOCK_DIR")
    if not root:
        return False
    lock_path = os.path.join(root, "workspace.turn.lock")
    if not os.path.exists(lock_path):
        return False
    try:
        from agents.task.utils import SafeFileLock

        probe = SafeFileLock(lock_path, timeout=0)
        try:
            probe.acquire()
        except Exception:
            return True  # held by someone else
        else:
            probe.release()
            return False
    except Exception:
        return False


# Persistent server/agent subcommands. A running one of these can open the DBs at any
# moment during the restore's os.replace window — even while holding NO lock right now
# (prod opens DB connections on demand and closes them between operations, so neither a
# write-lock probe nor an open-fd scan catches an idle-but-live server; only detecting
# the PROCESS does). `update` is deliberately excluded so a sibling `polyrob update`
# never counts as a server.
_SERVER_SUBCOMMANDS = frozenset({"telegram", "email", "serve", "api", "run", "chat", "gateway",
    "discord", "slack", "signal", "whatsapp", "x", "dashboard", "webgate"})


def _iter_proc_cmdlines():
    """Yield ``(pid, [argv...])`` for every process (Linux ``/proc``); empty elsewhere."""
    proc = Path("/proc")
    if not proc.is_dir():
        return
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except Exception:
            continue
        parts = [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]
        if parts:
            yield int(entry.name), parts


def _iter_psutil_cmdlines():
    """Yield ``(pid, [argv...])`` via psutil (portable); empty on any failure."""
    try:
        import psutil
    except Exception:
        return
    try:
        for p in psutil.process_iter(["pid", "cmdline"]):
            parts = p.info.get("cmdline") or []
            if parts:
                yield int(p.info["pid"]), list(parts)
    except Exception:
        return


def _parse_ps_lines(output: str):
    """Parse ``ps -axo pid=,args=`` output into ``(pid, [argv...])`` tuples.

    ``ps`` joins argv with spaces and does not quote, so a path WITH a space
    (a macOS home dir) cannot be split back exactly. Splitting on whitespace made
    ``/Users/A B/venv/bin/polyrob telegram`` read as ``[…/A, B/venv/bin/polyrob,
    telegram]`` — a false NEGATIVE for the guard. We keep the whitespace split
    (the only faithful choice) but ALSO re-join adjacent fragments up to the
    first token that ends in ``/polyrob`` so the executable is seen whole.
    """
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, rest = line.partition(" ")
        if not pid_str.isdigit():
            continue
        parts = rest.strip().split()
        if not parts:
            continue
        # `python …/polyrob telegram`: the interpreter is a whole token of its
        # own — never glue it onto the script path, or the executable rule in
        # server_process_alive() (position 1 ends in /polyrob) can't see it.
        start = 1 if len(parts) > 1 and Path(parts[0]).name.lower().startswith("python") else 0
        for i, tok in enumerate(parts):
            if i > start and tok.endswith("/polyrob") and "/" in "".join(parts[start:i]):
                # a split path: glue the fragments before it back on
                head = " ".join(parts[start:i + 1])
                if head.startswith("/") or head.startswith("~"):
                    parts = parts[:start] + [head] + parts[i + 1:]
                break
        yield int(pid_str), parts


def _iter_ps_cmdlines():
    """POSIX fallback (macOS/BSD without psutil): shell out to ``ps``."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-axo", "pid=,args="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return
    yield from _parse_ps_lines(out)


def _iter_cmdlines():
    """Portable process scan: /proc where it exists, else psutil, else ``ps``.

    U8 (2026-07-14 review): the scan was /proc-only, so on macOS the in-use guard
    was silently fail-open and --apply/--rollback would os.replace DBs under a
    live agent without --force.
    """
    if Path("/proc").is_dir():
        yield from _iter_proc_cmdlines()
        return
    try:
        import psutil  # noqa: F401 — availability probe
    except Exception:
        yield from _iter_ps_cmdlines()
        return
    yield from _iter_psutil_cmdlines()


def server_process_alive(*, exclude_pid: Optional[int] = None, _cmdlines=None) -> bool:
    """Best-effort: is a long-running POLYROB server/agent process running on this box?

    Portable scan (``/proc`` → psutil → ``ps``; fail-open on any error).
    ``_cmdlines`` is injectable for tests. This is the signal that actually
    protects a rollback on a server install, where the write-lock/open-fd probes clear
    against an idle-but-running service.
    """
    me = exclude_pid if exclude_pid is not None else os.getpid()
    src = _cmdlines if _cmdlines is not None else list(_iter_cmdlines())
    if _cmdlines is None and not src:
        raise ProcessScanUnavailable("Process inspection is unavailable; cannot verify that POLYROB is stopped.")
    try:
        for pid, parts in src:
            if pid == me:
                continue
            tokens = [t.lower() for t in parts]
            joined = " ".join(tokens)
            # The EXECUTABLE must be polyrob: the bare console script or a
            # `…/bin/polyrob` path. `t.endswith("polyrob")` also matched the
            # ARGUMENT `/opt/polyrob` — so `ls /opt/polyrob` or `tail -f
            # /opt/polyrob/x.log` blocked --apply and trained people onto --force.
            is_polyrob = (
                any(t == "polyrob" or t.endswith("/bin/polyrob") for t in tokens)
                # `python …/polyrob telegram`: the console script run through an
                # interpreter (the real prod argv shape) — position 1 only.
                or (len(tokens) > 1 and tokens[1].endswith("/polyrob")
                    and Path(tokens[0]).name.startswith("python"))
                or "cli.polyrob" in joined
                or "api.app" in joined
                or any(t.endswith("main.py") for t in tokens)
            )
            if not is_polyrob:
                continue
            if "update" in tokens:  # a sibling `polyrob update …`, not a server
                continue
            # Bare polyrob (possibly with root options) is the resident REPL.
            try:
                from cli.polyrob import _LAZY_SUBCOMMANDS
                known_commands = set(_LAZY_SUBCOMMANDS) | {"chat", "version"}
            except ImportError:
                known_commands = set(_SERVER_SUBCOMMANDS) | {"update"}
            bare_repl = not (set(tokens) & known_commands) and "--help" not in tokens and "--version" not in tokens
            if bare_repl or (set(tokens) & _SERVER_SUBCOMMANDS) or "uvicorn" in tokens \
                    or "api.app" in joined or any(t.endswith("main.py") for t in tokens):
                return True
    except Exception as exc:
        raise ProcessScanUnavailable(f"Process inspection failed: {exc}") from exc
    return False


def active_use_reasons(
    db_paths: Iterable[Path], *, lock_dir: Optional[Path] = None
) -> List[str]:
    """Human-readable reasons a restore is unsafe right now (empty = safe)."""
    reasons: List[str] = []
    for p in dbs_in_use(db_paths):
        reasons.append(f"database in use by another process: {p.name} ({p})")
    if workspace_lock_busy(lock_dir):
        reasons.append("a POLYROB session/turn is running (workspace.turn.lock held)")
    try:
        if server_process_alive():
            reasons.append("a POLYROB server/agent process is running (stop it before rollback)")
    except ProcessScanUnavailable as exc:
        reasons.append(str(exc))
    return reasons


@contextlib.contextmanager
def update_lock(root: Path, *, name: str = "update.lock"):
    """Exclusive, non-blocking flock serialising update/rollback in ``root``.

    Raises :class:`UpdateLockHeld` if another process holds it (never blocks — a
    second concurrent rollback should fail loudly, not queue behind the first).
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / name
    key = str(lock_path.resolve())

    with _HELD_LOCK:
        if key in _HELD:
            raise UpdateLockHeld(f"another update is in progress ({lock_path})")
        _HELD.add(key)

    lock = None
    try:
        try:
            from agents.task.utils import SafeFileLock

            lock = SafeFileLock(str(lock_path), timeout=0)
            lock.__enter__()  # raises TimeoutError on cross-process contention
        except ImportError:
            lock = None  # no flock backend — in-process guard still serialises
        except (TimeoutError, RuntimeError) as exc:
            lock = None
            raise UpdateLockHeld(f"another update is in progress ({lock_path})") from exc
        yield lock_path
    finally:
        if lock is not None:
            try:
                lock.__exit__(None, None, None)
            except Exception:
                pass
        with _HELD_LOCK:
            _HELD.discard(key)
