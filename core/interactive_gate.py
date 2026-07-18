"""Process-global 'a human is mid-turn' gate.

In the terminal REPL the user's interactive agent and the background autonomy
executors (goal dispatcher, cron) share ONE working directory (CWD). To avoid two
agents writing the same files at once, the REPL marks itself busy for the duration
of each turn, and the goal/cron executors skip a tick while busy (queued work runs
on the next idle tick). Inert on the server: no REPL ever marks busy there, so
``is_interactive_busy()`` stays False and server-side execution is unaffected.

A depth counter (not a bool) makes nested/re-entrant turns safe."""
from __future__ import annotations

import contextlib
import os
from typing import Optional

_busy_depth = 0


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
        return None
    return os.path.join(root, "workspace.turn.lock")


def _workspace_lock_timeout() -> float:
    try:
        return float(os.environ.get("CLI_WORKSPACE_LOCK_TIMEOUT", "30"))
    except ValueError:
        return 30.0


@contextlib.contextmanager
def workspace_turn_lock(timeout: Optional[float] = None):
    """Cross-process advisory lock around project-root workspace mutation (C2).

    The in-process busy-gate (below) only serializes a ticker against the live turn
    WITHIN one process; this file lock extends that across processes (a 2nd `rob`,
    or an autonomy ticker in another process). No-op unless enabled + configured.
    Blocking acquire; raises TimeoutError on contention (fail-loud serialization,
    never silent racing). On NFS the underlying flock is best-effort.
    """
    lp = _workspace_lock_path()
    if lp is None:
        yield None
        return
    from agents.task.utils import SafeFileLock

    to = _workspace_lock_timeout() if timeout is None else timeout
    with SafeFileLock(lp, timeout=to):
        yield lp


def mark_busy() -> None:
    global _busy_depth
    _busy_depth += 1


def mark_idle() -> None:
    global _busy_depth
    if _busy_depth > 0:
        _busy_depth -= 1


def is_interactive_busy() -> bool:
    return _busy_depth > 0


@contextlib.contextmanager
def interactive_turn():
    """Mark the process busy + hold the cross-process workspace lock for a turn.

    busy-depth serializes the in-process tickers; workspace_turn_lock serializes a
    second `rob` process / out-of-process ticker. On cross-process contention the
    lock raises TimeoutError (fail-loud) rather than racing the workspace files.
    """
    # Only the OUTERMOST turn acquires the (non-reentrant) cross-process lock;
    # nested turns are the same turn and must not re-acquire it.
    outermost = _busy_depth == 0
    mark_busy()
    try:
        if outermost:
            with workspace_turn_lock():
                yield
        else:
            yield
    finally:
        mark_idle()
