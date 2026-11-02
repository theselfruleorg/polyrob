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
        from core.file_lock import SafeFileLock

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
# 064 F1: every chat surface's standalone command (from its catalog row) is a server.
from core.surfaces.catalog import cli_commands as _surface_cli_commands  # noqa: E402

def _server_subcommands() -> frozenset:
    return frozenset({"serve", "api", "run", "chat", "gateway", "dashboard",
                      "webgate", *_surface_cli_commands()})


_SERVER_SUBCOMMANDS = _server_subcommands()


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


# Wrappers that run ANOTHER command: the guard looks through them to the real
# executable. An option VALUE (`sudo -u polyrob`, the account name) is never the
# executable — the 2026-10-29 managed install refused `sudo -u polyrob … update`
# because the token `polyrob` matched as if it were the agent.
_SUDO_VALUE_OPTS = frozenset("ugpCDhrtTUR")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_PY_VALUE_OPTS = frozenset({"-X", "-W", "-Q"})


def _skip_sudo(argv: List[str]) -> List[str]:
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return argv[i + 1:]
        if tok.startswith("--"):
            # --user=x carries its value; a bare --user takes the next token
            i += 1 if "=" in tok or tok in ("--login", "--shell", "--preserve-env",
                                             "--non-interactive", "--background") else 2
            continue
        if tok.startswith("-") and len(tok) > 1:
            letters = tok[1:]
            i += 1
            for pos, ch in enumerate(letters):
                if ch in _SUDO_VALUE_OPTS:
                    if pos == len(letters) - 1:
                        i += 1  # the value is the next token
                    break  # the rest of this token is the value
            continue
        return argv[i:]
    return []


def _skip_env(argv: List[str]) -> List[str]:
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
            i += 2
        elif tok.startswith("-") or "=" in tok:
            i += 1
        else:
            return argv[i:]
    return []


def _command_argv(parts: List[str], _depth: int = 0) -> List[str]:
    """The argv of the command a process really runs, wrappers removed."""
    import shlex

    argv = list(parts)
    while argv and _depth < 8:
        _depth += 1
        name = Path(argv[0]).name.lower()
        if name == "sudo":
            argv = _skip_sudo(argv)
        elif name == "env":
            argv = _skip_env(argv)
        elif name in ("nohup", "setsid", "exec", "nice", "ionice", "stdbuf"):
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[1:]
        elif name in _SHELLS:
            # `bash -lc "<command string>"` runs the string; other shell
            # invocations (a script, an interactive shell) are not POLYROB.
            for i, tok in enumerate(argv[1:], start=1):
                if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
                    if i + 1 >= len(argv):
                        return []
                    try:
                        inner = shlex.split(argv[i + 1])
                    except ValueError:
                        inner = argv[i + 1].split()
                    # the first command of a `a && b; c` string is enough
                    for j, t in enumerate(inner):
                        if t in ("&&", "||", ";", "|"):
                            inner = inner[:j]
                            break
                    argv = inner
                    break
                if not tok.startswith("-"):
                    return []
            else:
                return []
        else:
            return argv
    return argv


def _polyrob_args(argv: List[str]):
    """``(kind, args)`` for a POLYROB executable, else ``None``.

    ``kind`` is ``"cli"`` (args = the CLI argv after the executable) or
    ``"server"`` (an API server: uvicorn/gunicorn on ``api.app``, ``main.py``).
    Only the EXECUTABLE position counts: ``argv[0]``, or an interpreter and
    its script / ``-m`` module.
    """
    if not argv:
        return None
    exe = argv[0].lower()
    name = Path(exe).name
    if name == "polyrob":
        return "cli", [t.lower() for t in argv[1:]]
    if name in ("uvicorn", "gunicorn"):
        return ("server", []) if any("api.app" in t.lower() for t in argv[1:]) else None
    if not name.startswith("python"):
        return None
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "-m":
            if i + 1 >= len(argv):
                return None
            mod = argv[i + 1].lower()
            rest = [t.lower() for t in argv[i + 2:]]
            if mod == "cli.polyrob":
                return "cli", rest
            if mod in ("uvicorn", "gunicorn") and any("api.app" in t for t in rest):
                return "server", []
            return None
        if tok in _PY_VALUE_OPTS:
            i += 2
            continue
        if tok.startswith("-") and tok != "-":
            i += 1
            continue
        script = tok.lower()
        if Path(script).name == "polyrob":
            return "cli", [t.lower() for t in argv[i + 1:]]
        if Path(script).name == "main.py":
            return "server", []
        return None
    return None


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
            found = _polyrob_args(_command_argv(parts))
            if found is None:
                continue
            kind, args = found
            if kind == "server":
                return True
            if "update" in args:  # a sibling `polyrob update …`, not a server
                continue
            # Bare polyrob (possibly with root options) is the resident REPL.
            try:
                from cli.polyrob import _LAZY_SUBCOMMANDS
                known_commands = set(_LAZY_SUBCOMMANDS) | {"chat", "version"}
            except ImportError:
                known_commands = set(_server_subcommands()) | {"update"}
            bare_repl = not (set(args) & known_commands) and "--help" not in args and "--version" not in args
            if bare_repl or (set(args) & _server_subcommands()):
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
            from core.file_lock import SafeFileLock

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
