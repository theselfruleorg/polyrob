"""Process-global 'a human is mid-turn' gate.

In the terminal REPL the user's interactive agent and the background autonomy
executors (goal dispatcher, cron) share ONE working directory (CWD). To avoid two
agents writing the same files at once, the REPL marks itself busy for the duration
of each turn, and the goal/cron executors skip a tick while busy (queued work runs
on the next idle tick). Inert on the server: no REPL ever marks busy there, so
``is_interactive_busy()`` stays False and server-side execution is unaffected.

098 CO-01: the gate is a SET of live holders (token -> kind, session, human),
not a depth. Concurrent async turns (an owner DM and a room on one event loop)
do not nest, so a depth released the marker and the lock when the FIRST turn
ended. The `turn.active` marker and the workspace lock follow the set of HUMAN
holders (``turn_gate``: idle -> held -> idle); a goal run is a non-human holder
and never hides or suppresses an owner turn."""
from __future__ import annotations

import contextlib
import itertools
import logging
import os
import threading
from dataclasses import dataclass
from typing import Dict, Optional

from core.state.machine import StateMachine

logger = logging.getLogger(__name__)

TURN_GATE = StateMachine(
    "turn_gate",
    states=frozenset({"idle", "held"}),
    transitions={"idle": frozenset({"held"}), "held": frozenset({"held", "idle"})},
    initial=frozenset({"idle"}),
)


@dataclass(frozen=True)
class Holder:
    kind: str
    session_id: Optional[str]
    human: bool
    started: float


#: token -> live holder (owner turns, REPL turns, goal runs, woken runs).
_holders: Dict[int, Holder] = {}
_tokens = itertools.count(1)
_mutex = threading.RLock()
#: the workspace lock held while the gate is ``held`` (taken by the first human
#: holder, released by the last one — whichever context that is).
_gate_lock = None
_gate_state = "idle"


def _workspace_lock_path() -> Optional[str]:
    """Path of the cross-process workspace turn lock, or None if disabled.

    Gated by CLI_WORKSPACE_LOCK (default on) AND POLYROB_WORKSPACE_LOCK_DIR, which only
    build_cli_container sets — so this is a no-op on the server (no env => None).
    """
    from core.env import bool_env
    if not bool_env("CLI_WORKSPACE_LOCK", True):  # SSOT falsey set (incl. "none") — P4
        return None
    root = os.environ.get("POLYROB_WORKSPACE_LOCK_DIR")
    if not root:
        # 056 WS3: the headless server never set the lock dir, so its cron/goal
        # ticks and the deploy waiter could not see an owner turn at all. Derive
        # <data_dir>/locks when the data dir is known; a box with neither is a
        # bare dev checkout and stays lock-free as before.
        data = os.environ.get("POLYROB_DATA_DIR")
        if not data:
            return None
        root = os.path.join(data, "locks")
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            return None
    else:
        # The configured lock dir must exist too: without the `filelock` package
        # SafeFileLock's O_EXCL fallback raised FileNotFoundError on a fresh dir,
        # and the cron tick read that as "lock held" and deferred every due job.
        # An uncreatable dir keeps the path (the acquire then fails loud, as before).
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            pass
    return os.path.join(root, "workspace.turn.lock")


def turn_marker_path() -> Optional[str]:
    """`<POLYROB_DATA_DIR>/locks/turn.active` — the out-of-process fact "a human
    turn is live" (pid, kind, started, session_id). Read by
    `scripts/deploy_when_idle.sh`, the cron tick and the status snapshot; written
    only by :func:`owner_turn` / :func:`interactive_turn`.

    2026-09-19: the agent process carries POLYROB_WORKSPACE_LOCK_DIR=<project>/
    .polyrob, so the marker used to land there while every out-of-process reader
    looked under the data dir — a deploy fired at step 8 of a live owner turn
    (and at step 4 of another at 10:36Z). The per-workspace LOCK stays with the
    workspace; the MARKER lives under the data dir whenever one is known.

    077 F8 (2026-10-12): with no POLYROB_DATA_DIR (the self-host `systemd --user`
    unit) the marker used to fall back to the LOCK dir — ``<cwd>/.polyrob``, i.e.
    whatever the unit's WorkingDirectory was — while ``polyrob update --apply``
    (which builds no container, so has no lock dir) read
    ``<data_home>/locks/turn.active``: the updater could not see a live owner
    turn. The marker now resolves from the DATA HOME rule
    (``core.runtime_paths.resolve_data_home`` — POLYROB_HOME alone decides it), so
    the writer and every reader agree whatever the working directory is. A
    process with neither a data dir nor a lock dir (a bare checkout, a unit test)
    still writes no marker.
    """
    data = os.environ.get("POLYROB_DATA_DIR")
    if data:
        root = os.path.join(data, "locks")
    else:
        if _workspace_lock_path() is None:
            return None
        from core.runtime_paths import resolve_data_home
        root = os.path.join(str(resolve_data_home()), "locks")
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        return None
    return os.path.join(root, "turn.active")


def _write_turn_marker(humans, session_id: Optional[str] = None) -> None:
    """Write the marker for the live human holders: the oldest names ``kind`` /
    ``session_id`` / ``started`` (the shape every reader knows), ``holders``
    lists all; ``pid_start`` + ``boot_id`` make it a process identity (S5).
    ``humans`` may also be a kind string (one holder, the older call shape)."""
    if isinstance(humans, str):
        import time
        humans = [Holder(humans, session_id, True, time.time())]
    mp = turn_marker_path()
    first = humans[0]
    if mp is None:
        logger.info("turn marker NOT written (no marker path: lock dir/data dir unset) "
                    "kind=%s session=%s", first.kind, first.session_id)
        return
    try:
        from core.state.files import atomic_write_json
        from core.state.proc import process_identity
        me = process_identity()
        atomic_write_json(mp, {
            "pid": me.pid, "pid_start": me.start, "boot_id": me.boot,
            "kind": first.kind, "started": first.started,
            "session_id": first.session_id or "",
            "holders": [{"kind": h.kind, "session_id": h.session_id or ""} for h in humans],
        }, mode=0o644, indent=None)
        logger.info("turn marker written pid=%s kind=%s session=%s holders=%d path=%s",
                    me.pid, first.kind, first.session_id, len(humans), mp)
    except OSError:
        pass


def _clear_turn_marker() -> None:
    mp = turn_marker_path()
    if mp is None:
        return
    try:
        # One marker per data home: a second process's live turn may have
        # rewritten it — never clear THAT one (077 review).
        import json
        with open(mp, encoding="utf-8") as f:
            if int((json.load(f) or {}).get("pid") or 0) not in (0, os.getpid()):
                return
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        os.unlink(mp)
        logger.info("turn marker cleared pid=%s path=%s", os.getpid(), mp)
    except OSError:
        pass


def marker_is_live(m: dict) -> bool:
    """098 CO-01: a marker names a live turn only while its writer runs — pid,
    process start time and boot id all match (a reused pid is not proof of
    life). A marker without ``pid_start`` (an older writer) falls back to the
    pid check for one release."""
    pid = int(m.get("pid") or 0)
    if pid <= 0:
        return False
    if m.get("pid_start"):
        from core.state.proc import ProcIdentity, is_alive
        return is_alive(ProcIdentity(pid, str(m["pid_start"]), str(m.get("boot_id") or "")))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # alive, owned by another user
    return True


def read_turn_marker() -> Optional[dict]:
    """The live turn marker, or None (absent, unreadable, or from a process that
    no longer runs — a crashed process must not read as a turn forever)."""
    mp = turn_marker_path()
    if mp is None or not os.path.exists(mp):
        return None
    import json
    try:
        with open(mp, encoding="utf-8") as f:
            m = json.load(f)
        return m if marker_is_live(m) else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


#: Internal tuning (not env-driven): seconds to wait for the workspace lock.
CLI_WORKSPACE_LOCK_TIMEOUT = 30.0


def _workspace_lock_timeout() -> float:
    return CLI_WORKSPACE_LOCK_TIMEOUT


@contextlib.contextmanager
def workspace_turn_lock(timeout: Optional[float] = None):
    """Cross-process advisory lock around project-root workspace mutation (C2).

    The in-process busy-gate (below) only serializes a ticker against the live turn
    WITHIN one process; this file lock extends that across processes (a 2nd `polyrob`,
    or an autonomy ticker in another process). No-op unless enabled + configured.
    Blocking acquire; raises TimeoutError on contention (fail-loud serialization,
    never silent racing). On NFS the underlying flock is best-effort.
    """
    lp = _workspace_lock_path()
    if lp is None:
        yield None
        return
    from core.file_lock import SafeFileLock

    to = _workspace_lock_timeout() if timeout is None else timeout
    with SafeFileLock(lp, timeout=to):
        yield lp


def mark_busy(kind: str = "busy", session_id: Optional[str] = None, *,
              human: bool = False) -> int:
    """Register a holder; returns its token for :func:`mark_idle`."""
    import time
    with _mutex:
        token = next(_tokens)
        _holders[token] = Holder(kind, session_id, human, time.time())
        return token


def mark_idle(token: Optional[int] = None) -> None:
    """Release ``token``. An unknown token logs and releases nothing (it never
    ends someone else's turn). No token (an older caller that kept none)
    releases the newest NON-human holder only."""
    with _mutex:
        if token is None:
            for t in sorted(_holders, reverse=True):
                if not _holders[t].human:
                    _holders.pop(t)
                    return
            logger.warning("interactive gate: mark_idle() without a token and no non-human holder")
            return
        holder = _holders.pop(token, None)
        if holder is None:
            logger.warning("interactive gate: mark_idle(%s) — unknown token, nothing released", token)


def is_interactive_busy() -> bool:
    return bool(_holders)


def _humans():
    return [h for _, h in sorted(_holders.items()) if h.human]


def _gate_enter(token: int, *, refuse_on_contention: bool, lock_timeout: Optional[float]) -> None:
    """A human holder entered: ``idle -> held`` takes the lock and writes the
    marker; ``held -> held`` rewrites the marker with every live holder."""
    global _gate_lock, _gate_state
    with _mutex:
        humans = _humans()
        to = "held"
        TURN_GATE.check(_gate_state, to, ctx={"token": token})
        first = _gate_state == "idle"
        _gate_state = to
        _write_turn_marker(humans)
        if not first:
            return
        lp = _workspace_lock_path()
        if lp is None:
            return
        from core.file_lock import SafeFileLock
        timeout = _workspace_lock_timeout() if lock_timeout is None else lock_timeout
        lock = SafeFileLock(lp, timeout=timeout)
        try:
            lock.__enter__()
            _gate_lock = lock
        except Exception as e:
            if refuse_on_contention:
                raise
            logger.warning("owner turn proceeds without the workspace lock: %s", e)


def _gate_leave(token: int) -> None:
    """A human holder left: others remain -> rewrite the marker (``held``); the
    last one -> release the lock and clear the marker (``idle``)."""
    global _gate_lock, _gate_state
    with _mutex:
        mark_idle(token)
        humans = _humans()
        if humans:
            _write_turn_marker(humans)
            return
        if _gate_state == "held":
            TURN_GATE.check("held", "idle", ctx={"token": token})
        _gate_state = "idle"
        lock, _gate_lock = _gate_lock, None
        if lock is not None:
            try:
                lock.__exit__(None, None, None)
            except Exception:
                pass
        _clear_turn_marker()


@contextlib.contextmanager
def owner_turn(kind: str = "owner_chat", session_id: Optional[str] = None,
               lock_timeout: float = 0.0):
    """A human turn on a HEADLESS surface (Telegram/email owner turn): a human
    holder of the gate — the process is busy, the `turn.active` marker names it,
    and the first live human turn takes the cross-process lock — but NEVER
    refuses the human. If the lock is held the turn proceeds without it
    (logged); the holder and the marker still stop this process's cron/goal
    ticks and the deploy waiter. ``lock_timeout`` is 0: the acquire runs ON the
    event loop, and a cron or goal run holds the lock for its whole run, so a
    5 s wait stalled every session and then proceeded without the lock anyway
    (116 W1.3). Do not move the acquire to a thread: ``filelock`` is
    thread-local, and a release from another thread leaks the lock.

    056 WS3 (2026-09-19): until this existed the headless server's owner turns held
    nothing, so a money rail could run under the owner's live turn on the same
    shared workspace.
    """
    token = mark_busy(kind, session_id, human=True)
    try:
        _gate_enter(token, refuse_on_contention=False, lock_timeout=lock_timeout)
        yield
    finally:
        _gate_leave(token)


@contextlib.contextmanager
def interactive_turn():
    """A REPL turn: a human holder (busy, marker, cross-process lock).

    The first live human turn takes the lock; on cross-process contention it
    raises TimeoutError (fail-loud) rather than racing the workspace files. A
    goal run holding the gate (non-human) no longer suppresses the marker or
    the lock (098 CO-01).
    """
    token = mark_busy("repl", None, human=True)
    try:
        _gate_enter(token, refuse_on_contention=True, lock_timeout=None)
        yield
    finally:
        _gate_leave(token)


def reset_for_tests() -> None:
    """Drop every holder and the gate lock (test isolation only)."""
    global _gate_lock, _gate_state
    with _mutex:
        _holders.clear()
        lock, _gate_lock = _gate_lock, None
        _gate_state = "idle"
    if lock is not None:
        try:
            lock.__exit__(None, None, None)
        except Exception:
            pass
