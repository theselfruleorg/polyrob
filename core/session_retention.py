"""Session-tree retention (WS-K2, 2026-09-22) — the one sweep, plan then apply.

Prod on 2026-09-22: **2,449 session trees, 5.0 GB, 694 of them older than 30
days**, the oldest from 2026-07-19 — and no retention policy of any kind.
`memories` (365 d) and `episodes` (90 d) were swept on the curator tick; the
largest store on disk was swept by nobody. A third knob,
``AUTO_KNOWLEDGE_RETENTION_DAYS``, was declared in ``core/config.py`` with a
default of 7 and **zero consumers** — a retention setting that retained nothing.

⚠️ **This deletes directories.** Everything here is therefore built as PLAN then
APPLY: :func:`plan_sweep` is pure and returns what would go and what is kept
and WHY; :func:`apply_sweep` removes only what a plan named. Every owner seat
and the curator call the same two functions, so a dry run is the same code path
as the real one minus the last line.

Four things keep a tree, in the order they are cheapest to check:

1. **Age** — newer than the cutoff.
2. **Evidence** — an artifact row points inside it. An artifact is what the
   acceptance checks, the goal ledger and an invoice cite; deleting the file a
   receipt names turns a verified deliverable into "never produced".
3. **Live work** — a goal that is not in a terminal state names it.
4. **A live process** — the session registry has a row for it.

And two structural guards that are not policy: the path must resolve INSIDE the
session root, and a symlink is never followed and never removed. A retention
sweep that can be aimed by a symlink is an arbitrary-delete primitive.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

#: How many trees ONE sweep may remove. A backlog is drained over several
#: ticks rather than in one storm: the first run on prod would otherwise touch
#: 694 directories in a single pass while the agent is serving turns.
MAX_REMOVALS_PER_SWEEP = 200

KEEP_RECENT = "recent"
KEEP_ARTIFACT = "artifact"
KEEP_GOAL = "goal"
KEEP_REGISTERED = "registered"
KEEP_SYMLINK = "symlink"
KEEP_CAP = "cap"


@dataclass
class SweepPlan:
    root: str
    cutoff: float
    remove: List[str] = field(default_factory=list)
    keep: Dict[str, str] = field(default_factory=dict)   # path -> reason
    errors: List[str] = field(default_factory=list)

    @property
    def scanned(self) -> int:
        return len(self.remove) + len(self.keep)

    def kept_for(self, reason: str) -> int:
        return sum(1 for r in self.keep.values() if r == reason)


def retention_days() -> int:
    """Days a session tree is kept. ``<= 0`` disables the sweep entirely."""
    from core.config_policy.autonomy_config import AutonomyConfig
    return AutonomyConfig.session_retention_days()


#: The ONLY goal statuses that release a session tree. ⚠️ Stated as a DENY list
#: on purpose: the board also holds objectives and asks, each with its own
#: vocabulary (`open`/`fulfilled`/`rejected`/`obsolete` are all live on prod),
#: and an allow-list of "live" statuses would treat every word it had not heard
#: of as safe to delete. Here an unknown status PROTECTS.
TERMINAL_GOAL_STATUSES = ("done", "cancelled")

#: Where a session id can hide on a goal row. `session_id` is the column
#: `stamp_session` writes; the other three are what older rows carry in their
#: payload (the same names `webview/pages.py::_goal_session_id` reads).
_PAYLOAD_SESSION_KEYS = ("session_id", "run_session_id", "origin_session_id")


def _live_goal_sessions(data_dir: str) -> Set[str]:
    """Sessions named by a goal row that has not reached a terminal state.

    ⚠️ Fails CLOSED: an unreadable goal board must never read as "no goal needs
    any of these trees". The caller turns the raise into a refused sweep.
    """
    from core.status_snapshot import _rows

    placeholders = ",".join("?" for _ in TERMINAL_GOAL_STATUSES)
    try:
        rows = _rows(os.path.join(data_dir, "goals.db"),
                     "SELECT session_id, payload FROM goals "
                     f"WHERE status NOT IN ({placeholders})",
                     tuple(TERMINAL_GOAL_STATUSES))
    except FileNotFoundError:
        return set()
    except Exception as exc:
        raise RuntimeError(f"goals.db unreadable ({type(exc).__name__}: {exc})") from exc

    keep: Set[str] = set()
    for row in rows:
        keep.add(str(row.get("session_id") or ""))
        payload = row.get("payload")
        if not payload:
            continue
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
        except (TypeError, ValueError):
            # A payload we cannot parse might name a session. Unparseable is not
            # empty — but it is also not a reason to refuse the whole sweep, so
            # the row simply protects nothing beyond its column.
            continue
        if isinstance(data, dict):
            for key in _PAYLOAD_SESSION_KEYS:
                value = data.get(key)
                if value:
                    keep.add(str(value))
    return keep - {""}


def _registered_sessions(data_dir: str) -> Set[str]:
    """Sessions a live worker still owns. Fails CLOSED for the same reason."""
    from core.status_snapshot import _rows

    try:
        rows = _rows(os.path.join(data_dir, "session_registry.db"),
                     "SELECT session_id FROM active_sessions")
    except FileNotFoundError:
        return set()
    except Exception as exc:
        raise RuntimeError(
            f"session_registry.db unreadable ({type(exc).__name__}: {exc})") from exc
    return {str(r.get("session_id") or "") for r in rows} - {""}


def _artifact_dirs(data_dir: str) -> Set[str]:
    """Resolved directories that hold at least one registered artifact."""
    from core.status_snapshot import _rows

    dirs: Set[str] = set()
    db = os.path.join(data_dir, "artifacts.db")
    try:
        rows = _rows(db, "SELECT path FROM artifacts")
    except FileNotFoundError:
        return dirs
    except Exception as exc:
        raise RuntimeError(f"artifacts.db unreadable ({type(exc).__name__}: {exc})") from exc
    for row in rows:
        path = str(row.get("path") or "")
        if path:
            dirs.add(os.path.normpath(path))
    return dirs


def _holds_artifact(tree: Path, artifact_paths: Set[str]) -> bool:
    prefix = os.path.normpath(str(tree)) + os.sep
    return any(p.startswith(prefix) for p in artifact_paths)


def plan_sweep(data_dir: str, *, days: Optional[int] = None,
               now: Optional[float] = None,
               sessions_root: Optional[str] = None,
               max_removals: int = MAX_REMOVALS_PER_SWEEP) -> SweepPlan:
    """What a sweep WOULD do. Pure: reads stores and stats, changes nothing.

    ⚠️ The root is ``<data_dir>/sessions`` (or an explicit ``DATA_ROOT``), and it
    must resolve strictly INSIDE the data home. The server resolver's legacy
    ``./data/task`` fallback is never used here: in the local CLI that is a
    directory of the user's own project. Only UUID-named session trees are
    candidates, and age is the newest mtime anywhere in the tree.
    """
    from core.session_gc import _is_uuid, _newest_mtime_and_size

    now = float(now if now is not None else time.time())
    days = int(retention_days() if days is None else days)
    data_home = Path(data_dir).resolve()
    if sessions_root:
        root = Path(sessions_root)
    else:
        explicit = os.getenv("DATA_ROOT")
        root = Path(explicit) if explicit and explicit.strip() else data_home / "sessions"
    plan = SweepPlan(root=str(root), cutoff=now - days * 86400)
    try:
        inside = root.resolve().relative_to(data_home) != Path(".")
    except ValueError:
        inside = False
    if not inside:
        plan.errors.append(
            f"refused: session root {root} is not inside the data home {data_home}")
        return plan
    if days <= 0:
        plan.errors.append(
            "session retention is OFF — no tree is ever deleted. It is a\n"
            "    deploy-time setting, not a preference: set SESSION_RETENTION_DAYS\n"
            "    to a number of days in the env file, or pass --days here for one run.")
        return plan
    if not root.exists():
        plan.errors.append(f"no session root at {root}")
        return plan

    goal_sessions = _live_goal_sessions(data_dir)
    registered = _registered_sessions(data_dir)
    artifacts = _artifact_dirs(data_dir)
    root_resolved = root.resolve()

    for tenant in sorted(os.scandir(root), key=lambda e: e.name):
        if not tenant.is_dir(follow_symlinks=False):
            continue
        for entry in sorted(os.scandir(tenant.path), key=lambda e: e.name):
            if entry.is_symlink():
                plan.keep[entry.path] = KEEP_SYMLINK
                continue
            if not entry.is_dir() or not _is_uuid(entry.name):
                continue  # only session trees; never `sessions`, `workspace`, …
            tree = Path(entry.path)
            # Structural guard: never leave the root, whatever the name says.
            try:
                tree.resolve().relative_to(root_resolved)
            except ValueError:
                plan.keep[entry.path] = KEEP_SYMLINK
                continue
            if entry.name in registered:
                plan.keep[entry.path] = KEEP_REGISTERED
                continue
            if entry.name in goal_sessions:
                plan.keep[entry.path] = KEEP_GOAL
                continue
            # The newest file anywhere in the tree decides — a live session can
            # leave its top-level directory untouched for months.
            mtime, _size = _newest_mtime_and_size(entry.path)
            if mtime <= 0:
                plan.errors.append(f"{entry.path}: could not stat; kept")
                plan.keep[entry.path] = KEEP_RECENT
                continue
            if mtime >= plan.cutoff:
                plan.keep[entry.path] = KEEP_RECENT
                continue
            if _holds_artifact(tree, artifacts):
                plan.keep[entry.path] = KEEP_ARTIFACT
                continue
            if len(plan.remove) >= max(0, int(max_removals)):
                plan.keep[entry.path] = KEEP_CAP
                continue
            plan.remove.append(entry.path)
    return plan


def apply_sweep(plan: SweepPlan) -> Dict[str, int]:
    """Remove exactly what *plan* named. Returns ``{removed, failed}``."""
    removed = failed = 0
    root = os.path.normpath(plan.root) + os.sep
    for path in plan.remove:
        norm = os.path.normpath(path)
        if not norm.startswith(root) or os.path.islink(norm) or not os.path.isdir(norm):
            plan.errors.append(f"{path}: refused (outside root, a link, or not a dir)")
            failed += 1
            continue
        try:
            shutil.rmtree(norm)
            removed += 1
        except OSError as exc:
            plan.errors.append(f"{path}: {type(exc).__name__}: {exc}")
            failed += 1
    return {"removed": removed, "failed": failed}


def sweep(data_dir: str, *, days: Optional[int] = None,
          now: Optional[float] = None) -> Dict[str, int]:
    """Plan and apply in one call — what the curator tick runs."""
    plan = plan_sweep(data_dir, days=days, now=now)
    if not plan.remove:
        return {"removed": 0, "failed": 0, "scanned": plan.scanned}
    out = apply_sweep(plan)
    out["scanned"] = plan.scanned
    return out


__all__ = ["MAX_REMOVALS_PER_SWEEP", "TERMINAL_GOAL_STATUSES", "SweepPlan",
           "apply_sweep", "plan_sweep", "retention_days", "sweep"]
