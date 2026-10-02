"""Perceive the agent's state before an update, and wait for the wrap-up (058).

``polyrob update --apply`` used to refuse the moment anything looked busy and
tell the operator to stop the agent. A code swap under a running turn, a goal
run or a cron rail is the harm; a turn that ends in forty seconds is not a
reason to give up. This module reads the agent-activity signals
``scripts/deploy_when_idle.sh`` reads before a prod deploy, and waits — bounded —
until they clear. (The shell's two DEPLOY-side gates — the owner-DM veto over
``inbound_routed`` and the ``.deploy.lock`` of a concurrent deployer — are not
agent activity and are not read here; the cron-imminent horizon is 120 s to the
shell's 60 s, a wider "busy".)

- a live human turn: ``<data>/locks/turn.active`` (dead-pid aware, via
  ``core.interactive_gate.read_turn_marker``);
- a goal ``running`` (``goals.db``);
- a cron job ``running``, or one due within ``cron_imminent_sec`` (a job due in
  30 s is as busy as a running one — the scheduler's next tick would start it
  mid-swap);
- the cross-process workspace turn lock;
- a write-locked SQLite store.

⚠️ An UNREADABLE store is BUSY, never idle: a reader deciding whether to swap
code under an agent may not guess. Absent stores are simply not signals (a
fresh install has no goals.db). A resident server PROCESS is a different
fact — waiting does not end it — so it is not part of ``idle`` here; the
caller reports it separately (``process_guard.server_process_alive``).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

#: A cron job due within this many seconds counts as running.
DEFAULT_CRON_IMMINENT_SEC = 120
DEFAULT_WAIT_SEC = 1800
DEFAULT_POLL_SEC = 15


@dataclass
class AgentActivity:
    reasons: List[str] = field(default_factory=list)      # why it is busy, in plain words
    unreadable: List[str] = field(default_factory=list)   # stores that could not be read (busy)
    waited: float = 0.0

    @property
    def idle(self) -> bool:
        return not self.reasons and not self.unreadable

    def describe(self) -> str:
        if self.idle:
            return "idle"
        return "; ".join(self.reasons + [f"unreadable: {u}" for u in self.unreadable])


def _count(db: Path, sql: str, params: tuple = ()) -> Optional[int]:
    """A read-only count, or raise. Never creates the file (the caller checks)."""
    import sqlite3
    uri = f"file:{db}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=3)
    try:
        row = con.execute(sql, params).fetchone()
        return int(row[0] if row and row[0] is not None else 0)
    finally:
        con.close()


def _turn_reason(data_home: Path) -> Optional[str]:
    from core.interactive_gate import read_turn_marker
    m = read_turn_marker()
    if not m:
        # read_turn_marker resolves the path from the env; also honour the
        # explicit home so a test / an admin call is not env-dependent.
        mp = data_home / "locks" / "turn.active"
        if not mp.exists():
            return None
        import json
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
            pid = int(m.get("pid") or 0)
            # A marker with no positive pid is not a turn (the shell reader's
            # `[ "$pid" -gt 0 ]`); os.kill(0, 0) would signal OUR OWN process
            # group and succeed, reading as a phantom live turn.
            if pid <= 0:
                return None
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except (OSError, ValueError, TypeError):
            return "a live turn marker that cannot be parsed (treated as a live turn)"
    kind = str(m.get("kind") or "turn")
    sid = str(m.get("session_id") or "")[:12]
    # The writer (core.interactive_gate._write_turn_marker) stamps ``started``;
    # ``since`` is accepted for an older marker shape.
    since = m.get("started", m.get("since"))
    age = f", {int(time.time() - float(since))} s" if isinstance(since, (int, float)) else ""
    return f"a live human turn ({kind}{', session ' + sid if sid else ''}{age})"


def observe(data_home: Path, *, cron_imminent_sec: int = DEFAULT_CRON_IMMINENT_SEC) -> AgentActivity:
    """One read of every busy signal."""
    data_home = Path(data_home)
    act = AgentActivity()

    reason = _turn_reason(data_home)
    if reason:
        act.reasons.append(reason)

    goals = data_home / "goals.db"
    if goals.exists():
        try:
            n = _count(goals, "SELECT count(*) FROM goals WHERE status IN ('running','claimed')")
            if n:
                act.reasons.append(f"{n} goal(s) running")
        except Exception as e:
            act.unreadable.append(f"goals.db ({type(e).__name__}: {e})")

    cron = data_home / "cron.db"
    if cron.exists():
        try:
            n = _count(cron, "SELECT count(*) FROM cron_jobs WHERE status = 'running'")
            if n:
                act.reasons.append(f"{n} cron job(s) running")
            horizon = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + cron_imminent_sec))
            n = _count(cron, "SELECT count(*) FROM cron_jobs WHERE status = 'scheduled' AND enabled = 1 "
                             "AND next_run_at IS NOT NULL AND next_run_at <= ?", (horizon,))
            if n:
                act.reasons.append(f"{n} cron job(s) due within {cron_imminent_sec} s")
        except Exception as e:
            act.unreadable.append(f"cron.db ({type(e).__name__}: {e})")

    try:
        from cli.update.process_guard import workspace_lock_busy
        if workspace_lock_busy():
            act.reasons.append("a session turn holds the workspace lock")
    except Exception as e:
        act.unreadable.append(f"workspace lock ({type(e).__name__}: {e})")
    return act


def wait_for_wrap_up(data_home: Path, *, timeout: float = DEFAULT_WAIT_SEC,
                     poll: float = DEFAULT_POLL_SEC,
                     on_tick: Optional[Callable[[AgentActivity, float], None]] = None,
                     cron_imminent_sec: int = DEFAULT_CRON_IMMINENT_SEC) -> AgentActivity:
    """Poll ``observe`` until idle or *timeout*; the result carries ``waited``.
    ``on_tick(activity, waited)`` is called on every busy observation so the
    operator sees WHAT the update is waiting on, not a silent pause."""
    start = time.monotonic()
    while True:
        act = observe(data_home, cron_imminent_sec=cron_imminent_sec)
        act.waited = time.monotonic() - start
        if act.idle or act.waited >= timeout:
            return act
        if on_tick is not None:
            on_tick(act, act.waited)
        time.sleep(poll)
