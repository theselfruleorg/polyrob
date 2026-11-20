"""The ONE "is the agent busy" reader (058, proposal 125 S13).

Three callers ask the same question — may the code be swapped or the process
restarted now? — and all three read it here:

- the 077 drain (``cli/commands/_lifecycle.py``), without the "due soon" signal;
- ``polyrob update --apply`` (``cli/update/agent_state.py`` re-exports this and
  adds the bounded wait);
- the prod deploy waiter ``scripts/deploy_when_idle.sh``, which runs
  ``python -m core.agent_activity --json`` through the venv python. Its
  deploy-only gates (the owner-DM veto, ``.deploy.lock``, ``/update now``) are
  not agent activity and stay in the script.

The signals:

- a live human turn: ``<data>/locks/turn.active`` (pid + start time + boot id,
  ``core.interactive_gate.marker_is_live``);
- a goal in a busy status (``GOAL_BUSY_STATUSES``, from ``core.goal_vocab``);
- a cron job ``running``, or one due within ``cron_imminent_sec`` (a job due in
  30 s is as busy as a running one — the scheduler's next tick would start it
  mid-swap);
- the cross-process workspace turn lock.

⚠️ An UNREADABLE store is BUSY, never idle: a reader deciding whether to swap
code under an agent may not guess. Absent stores are simply not signals (a
fresh install has no goals.db). A marker that is not a plain file (a link, a
FIFO) is a live turn: the deploy waiter runs this as root over a group-writable
``locks`` dir, so the marker is opened without following a link and without
blocking. A resident server PROCESS is a different fact — waiting does not end
it — so it is not part of ``idle`` here.

⚠️ The deploy waiter runs THIS module from the new checkout on the OLD venv (the
new dependencies install after the wait). Keep its import surface — this module,
``core.goal_vocab``, ``core.runtime_paths``, ``core.interactive_gate``,
``core.file_lock`` and what ``import core`` loads — on dependencies the previous
release already had; a new top-level dependency there makes every poll
"unreadable", and no deploy lands until someone deploys by hand.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

from core.goal_vocab import STATUS_RUNNING
from core.runtime_paths import cron_db_path, goals_db_path, resolve_data_home

logger = logging.getLogger(__name__)

#: A cron job due within this many seconds counts as running.
DEFAULT_CRON_IMMINENT_SEC = 120
#: The goal statuses that mean "a run is in flight" (core.goal_vocab).
GOAL_BUSY_STATUSES = (STATUS_RUNNING,)
#: The cron job status that means "a run is in flight" (core/cron_store.py).
CRON_BUSY_STATUS = "running"
#: The cron job status a due job waits in.
CRON_SCHEDULED_STATUS = "scheduled"

UNREADABLE = "unreadable"

Signal = Union[int, str]


@dataclass
class AgentActivity:
    reasons: List[str] = field(default_factory=list)      # why it is busy, in plain words
    unreadable: List[str] = field(default_factory=list)   # stores that could not be read (busy)
    waited: float = 0.0
    #: One value per signal: a count, or "unreadable" (the deploy waiter prints them).
    signals: Dict[str, Signal] = field(default_factory=dict)

    @property
    def idle(self) -> bool:
        return not self.reasons and not self.unreadable

    def describe(self) -> str:
        if self.idle:
            return "idle"
        return "; ".join(self.reasons + [f"unreadable: {u}" for u in self.unreadable])

    def to_json(self) -> dict:
        return {"idle": self.idle, "reasons": list(self.reasons),
                "unreadable": list(self.unreadable), "signals": dict(self.signals)}


def _count(db: Path, sql: str, params: tuple = ()) -> int:
    """A read-only count, or raise. Never creates the file (the caller checks)."""
    import sqlite3
    uri = f"file:{db}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=3)
    try:
        row = con.execute(sql, params).fetchone()
        return int(row[0] if row and row[0] is not None else 0)
    finally:
        con.close()


def _marker_candidates(data_home: Path) -> List[Path]:
    """Where a live turn marker may sit, primary first.

    077 F8: before the marker followed the data home, a gateway with no
    POLYROB_DATA_DIR wrote it beside its workspace LOCK — ``<WorkingDirectory>/
    .polyrob/turn.active``, which for the ``polyrob service`` user unit
    (WorkingDirectory=$HOME) is ``~/.polyrob/turn.active``. An update run against
    a gateway still on that older code must see its turn there too.
    """
    out = [data_home / "locks" / "turn.active"]
    try:
        from core.interactive_gate import turn_marker_path
        env_path = turn_marker_path()
        if env_path:
            out.append(Path(env_path))
    except Exception:
        pass
    if not (os.environ.get("POLYROB_DATA_DIR") or "").strip():
        # A data-dir install (prod) always wrote the marker under it.
        try:
            from core.paths import polyrob_home
            out.append(Path(polyrob_home()) / "turn.active")
        except Exception:
            pass
        out.append(Path.home() / ".polyrob" / "turn.active")
    seen, uniq = set(), []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


def _read_no_follow(mp: Path) -> Optional[str]:
    """The text of a plain file, or None when absent. Never follows a link at the
    last step, never blocks on a FIFO; anything but a plain file raises OSError."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        st = os.lstat(mp)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"{mp.name} is not a plain file")
    fd = os.open(mp, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{mp.name} is not a plain file")
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
            if sum(map(len, chunks)) > 1 << 20:
                raise OSError(f"{mp.name} is too large")
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(fd)


def _read_marker_file(mp: Path):
    """``(marker, None)`` for a live marker, ``(None, None)`` for none/dead, or
    ``(None, reason)`` for one that cannot be read or parsed (a live turn)."""
    try:
        raw = _read_no_follow(Path(mp))
    except (OSError, UnicodeDecodeError):
        return None, "a live turn marker that cannot be read (treated as a live turn)"
    if raw is None:
        return None, None
    try:
        m = json.loads(raw)
        # A marker with no positive pid is not a turn. 098 CO-01: live only while
        # pid + start time + boot id match (a reused pid after a crash is not a
        # turn); a marker without a start time falls back to the pid check.
        from core.interactive_gate import marker_is_live
        if not marker_is_live(m):
            return None, None
    except (OSError, ValueError, TypeError, AttributeError):
        return None, "a live turn marker that cannot be parsed (treated as a live turn)"
    return m, None


def _turn_reason(data_home: Path) -> Optional[str]:
    m = None
    for mp in _marker_candidates(Path(data_home)):
        m, bad = _read_marker_file(mp)
        if bad:
            return bad
        if m:
            break
    if not m:
        return None
    kind = str(m.get("kind") or "turn")
    sid = str(m.get("session_id") or "")[:12]
    # The writer (core.interactive_gate._write_turn_marker) stamps ``started``;
    # ``since`` is accepted for an older marker shape.
    since = m.get("started", m.get("since"))
    age = f", {int(time.time() - float(since))} s" if isinstance(since, (int, float)) else ""
    return f"a live human turn ({kind}{', session ' + sid if sid else ''}{age})"


def workspace_lock_busy(lock_dir: Optional[Path] = None) -> bool:
    """True if the cross-process workspace turn lock is currently held.

    Uses the CLI's ``POLYROB_WORKSPACE_LOCK_DIR`` convention when ``lock_dir`` is not
    given. Inert (returns False) when no lock is configured. Only a TimeoutError means
    "held"; any other error is RAISED (a guard must not read "unknown" as "free"), and
    each caller names it.
    """
    root = str(lock_dir) if lock_dir else os.environ.get("POLYROB_WORKSPACE_LOCK_DIR")
    if not root:
        return False
    lock_path = os.path.join(root, "workspace.turn.lock")
    if not os.path.exists(lock_path):
        return False
    from core.file_lock import SafeFileLock

    try:
        with SafeFileLock(lock_path, timeout=0):
            return False
    except TimeoutError:
        return True  # held by someone else
    except Exception as exc:
        logger.warning("workspace lock probe failed on %s: %s", lock_path, exc)
        raise


def observe(data_home: Optional[Path] = None, *,
            cron_imminent_sec: Optional[int] = DEFAULT_CRON_IMMINENT_SEC) -> AgentActivity:
    """One read of every busy signal. ``data_home=None`` = the data home
    (``core.runtime_paths.resolve_data_home``).

    ``cron_imminent_sec=None`` drops the "due soon" signal: a draining process
    (077) starts no new job, so a due one would keep it busy for ever."""
    data_home = Path(data_home) if data_home is not None else Path(resolve_data_home())
    act = AgentActivity()

    reason = _turn_reason(data_home)
    act.signals["turn"] = 1 if reason else 0
    if reason:
        act.reasons.append(reason)

    goals = Path(goals_db_path(str(data_home)))
    act.signals["goals_running"] = 0
    if goals.exists():
        try:
            marks = ",".join("?" * len(GOAL_BUSY_STATUSES))
            n = _count(goals, f"SELECT count(*) FROM goals WHERE status IN ({marks})",
                       GOAL_BUSY_STATUSES)
            act.signals["goals_running"] = n
            if n:
                act.reasons.append(f"{n} goal(s) running")
        except Exception as e:
            act.signals["goals_running"] = UNREADABLE
            act.unreadable.append(f"goals.db ({type(e).__name__}: {e})")

    cron = Path(cron_db_path(str(data_home)))
    act.signals["cron_running"] = 0
    if cron_imminent_sec is not None:
        act.signals["cron_imminent"] = 0
    if cron.exists():
        try:
            n = _count(cron, "SELECT count(*) FROM cron_jobs WHERE status = ?", (CRON_BUSY_STATUS,))
            act.signals["cron_running"] = n
            if n:
                act.reasons.append(f"{n} cron job(s) running")
        except Exception as e:
            act.signals["cron_running"] = UNREADABLE
            act.unreadable.append(f"cron.db ({type(e).__name__}: {e})")
        if cron_imminent_sec is not None:
            try:
                # The store's own clock and format: next_run_at is naive LOCAL time
                # (CronService), so a UTC horizon was off by the box's UTC offset.
                from datetime import datetime, timedelta
                horizon = (datetime.now() + timedelta(seconds=cron_imminent_sec)).isoformat()
                n = _count(cron, "SELECT count(*) FROM cron_jobs WHERE status = ? "
                                 "AND enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?",
                           (CRON_SCHEDULED_STATUS, horizon))
                act.signals["cron_imminent"] = n
                if n:
                    act.reasons.append(f"{n} cron job(s) due within {cron_imminent_sec} s")
            except Exception as e:
                act.signals["cron_imminent"] = UNREADABLE
                act.unreadable.append(f"cron.db due jobs ({type(e).__name__}: {e})")

    try:
        busy = workspace_lock_busy()
        act.signals["workspace_lock"] = 1 if busy else 0
        if busy:
            act.reasons.append("a session turn holds the workspace lock")
    except Exception as e:
        act.signals["workspace_lock"] = UNREADABLE
        act.unreadable.append(f"workspace lock ({type(e).__name__}: {e})")
    return act


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m core.agent_activity [--json] [--data DIR] [--imminent SEC]``.

    Exit 0 when every signal was read (idle or busy: the output says which), 2 when
    a signal could not be read (the output is still printed, and it is not idle)."""
    ap = argparse.ArgumentParser(prog="python -m core.agent_activity",
                                 description="Is the agent busy? One read of every signal.")
    ap.add_argument("--json", action="store_true", help="print one JSON object")
    ap.add_argument("--data", default=None, help="the data home (default: the resolved one)")
    ap.add_argument("--imminent", type=int, default=DEFAULT_CRON_IMMINENT_SEC,
                    help="seconds: a cron job due this soon is busy (negative: ignore due jobs)")
    args = ap.parse_args(argv)
    imminent = None if args.imminent < 0 else args.imminent
    act = observe(Path(args.data) if args.data else None, cron_imminent_sec=imminent)
    if args.json:
        print(json.dumps(act.to_json(), sort_keys=True))
    else:
        print(act.describe())
    return 2 if act.unreadable else 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    sys.exit(main())
