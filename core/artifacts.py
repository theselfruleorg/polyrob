"""The artifact ledger: one durable record per file the agent produces.

Before this, an artifact was a filename in a log line. Nothing owned it, nothing
knew whether it still existed, and four subsystems each guessed at it
independently — ``goals/deliverables.py`` scanned a time window on a shared
workspace, the goal acceptance checker ran ``file_contains`` against a relative
path, the completion judge compared a byte count the agent itself claimed, and an
x402 invoice cited a research packet by name. When the daily workspace cleanup
deleted the shared project root (2026-08-16/17), every one of those guesses
silently became wrong: round N+1 failed "file not found" on evidence round N had
really written.

A row is written at write time and carries the facts none of those consumers can
recover afterwards — who produced it, where it lives, and what it contained. That
turns "does the evidence exist?" from a filesystem guess into ``verify()``.

Deliberately NOT stored here: file CONTENT (the row points at the file; content
lives in the KB when the agent chooses to ingest it) and VERSION history (a
re-write updates the hash in place). Both are YAGNI until a consumer asks.

Tenant scoping is structural: every read, every write and every verdict takes a
``user_id`` and filters on it, so a caller cannot forget the filter.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)

# Verdicts returned by verify(). "unknown" covers both "no such id" and "not
# yours" on purpose — a cross-tenant probe must not be able to distinguish them.
VERIFY_OK = "ok"
VERIFY_MISSING = "missing"
VERIFY_CHANGED = "changed"
VERIFY_UNKNOWN = "unknown"

# What the artifact IS, which decides what the ship rail may do with it.
KIND_PAGE = "page"      # servable as a static page
KIND_CODE = "code"
KIND_REPORT = "report"
KIND_DATA = "data"
KIND_FILE = "file"      # default: something produced, kind not asserted

_HASH_CHUNK = 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL DEFAULT '',
    goal_id     TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'file',
    sha256      TEXT NOT NULL DEFAULT '',
    bytes       INTEGER NOT NULL DEFAULT 0,
    url         TEXT,
    created_at  REAL NOT NULL,
    verified_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_ident ON artifacts(user_id, path);
CREATE INDEX IF NOT EXISTS idx_artifacts_goal    ON artifacts(user_id, goal_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(user_id, session_id);
"""


@dataclass
class Artifact:
    id: str
    user_id: str
    session_id: str
    goal_id: str
    path: str
    kind: str
    sha256: str
    bytes: int
    url: Optional[str]
    created_at: float
    verified_at: Optional[float]

    @classmethod
    def from_row(cls, row: Any) -> "Artifact":
        d = dict(row)
        return cls(
            id=d["id"], user_id=d["user_id"], session_id=d["session_id"],
            goal_id=d["goal_id"], path=d["path"], kind=d["kind"],
            sha256=d["sha256"], bytes=int(d["bytes"] or 0), url=d["url"],
            created_at=float(d["created_at"] or 0.0),
            verified_at=d["verified_at"],
        )


def default_artifacts_db() -> str:
    """Resolve ``artifacts.db`` next to its sibling autonomy DBs.

    ``ARTIFACTS_DB_PATH`` always wins — the seam the test suite uses to keep this
    DB out of the developer's real data home. Mirrors
    ``hf_deploy/registry.py::default_deployed_apps_db``.
    """
    override = os.getenv("ARTIFACTS_DB_PATH")
    if override:
        return override
    try:
        from core.container import DependencyContainer
        cfg = DependencyContainer.get_instance().get_service("config")
        data_dir = getattr(cfg, "data_dir", None)
        if data_dir:
            return os.path.join(str(data_dir), "artifacts.db")
    except Exception:
        pass
    from core.runtime_config import get_data_root
    return os.path.join(get_data_root(), "artifacts.db")


def hash_file(path: str) -> tuple[str, int]:
    """(sha256 hex, byte length) for *path*, read in chunks so a large artifact
    never has to fit in memory."""
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


class ArtifactLedger:
    """WAL-backed store of one row per produced file, scoped by tenant."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or default_artifacts_db()
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        conn = wal_connect(self.db_path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # --- writes ----------------------------------------------------------

    def record(self, user_id: str, path: str, *, session_id: str = "",
               goal_id: str = "", kind: str = KIND_FILE) -> Optional[Artifact]:
        """Record (or refresh) the artifact at *path* for *user_id*.

        Identity is ``(user_id, path)``, so a re-write of the same file updates
        the existing row rather than growing a second one — the agent editing its
        own report across three rounds is ONE artifact, not three.

        Returns None when the file does not exist: an artifact is a thing that is
        actually on disk, and a row promising otherwise is exactly the lie this
        module exists to stop.
        """
        try:
            # realpath (not abspath) so the stored key matches the external
            # callers that look a row up by realpath (tools/publish, deliverables)
            # — otherwise a symlinked data home / macOS /tmp->/private/tmp made
            # published_url_for miss the row and the URL never stamped.
            abspath = os.path.realpath(path)
            if not os.path.isfile(abspath):
                return None
            sha, size = hash_file(abspath)
        except OSError as e:
            logger.debug("artifact record skipped for %s: %s", path, e)
            return None

        now = time.time()
        existing = self._row_by_path(user_id, abspath)
        if existing is not None:
            execute_retry(
                self.db_path,
                "UPDATE artifacts SET sha256=?, bytes=?, kind=?, session_id=?, "
                "goal_id=?, verified_at=? WHERE id=? AND user_id=?",
                (sha, size, kind or existing["kind"],
                 session_id or existing["session_id"],
                 goal_id or existing["goal_id"], now,
                 existing["id"], user_id),
            )
            return self.get(existing["id"], user_id)

        artifact_id = uuid.uuid4().hex[:12]
        execute_retry(
            self.db_path,
            "INSERT INTO artifacts (id, user_id, session_id, goal_id, path, kind, "
            "sha256, bytes, url, created_at, verified_at) "
            "VALUES (?,?,?,?,?,?,?,?,NULL,?,?)",
            (artifact_id, user_id, session_id, goal_id, abspath, kind,
             sha, size, now, now),
        )
        return self.get(artifact_id, user_id)

    def attach_goal(self, user_id: str, session_id: str, goal_id: str) -> int:
        """Stamp this session's UNATTRIBUTED artifacts with *goal_id*.

        The tools tier may not know what a goal is (the layering ratchet forbids
        ``tools`` -> ``agents.task.goals``), so a write records
        ``(user_id, session_id)`` only. The dispatcher owns the goal<->session
        mapping and calls this at run end.

        Already-attributed rows are left alone: a session reused by a second goal
        must not re-steal the first goal's evidence. Returns the number stamped.
        """
        if not (user_id and session_id and goal_id):
            return 0
        rowcount = execute_retry(
            self.db_path,
            "UPDATE artifacts SET goal_id=? WHERE user_id=? AND session_id=? AND goal_id=''",
            (goal_id, user_id, session_id),
        )
        return int(rowcount or 0)

    def set_url(self, artifact_id: str, user_id: str, url: str) -> bool:
        """Attach a published URL. False when the artifact is not this tenant's."""
        if self.get(artifact_id, user_id) is None:
            return False
        execute_retry(
            self.db_path,
            "UPDATE artifacts SET url=? WHERE id=? AND user_id=?",
            (url, artifact_id, user_id),
        )
        return True

    # --- reads -----------------------------------------------------------

    def _row_by_path(self, user_id: str, abspath: str) -> Optional[Dict[str, Any]]:
        row = execute_retry(
            self.db_path,
            "SELECT * FROM artifacts WHERE user_id=? AND path=?",
            (user_id, abspath), fetch="one",
        )
        return dict(row) if row else None

    def get(self, artifact_id: str, user_id: str) -> Optional[Artifact]:
        row = execute_retry(
            self.db_path,
            "SELECT * FROM artifacts WHERE id=? AND user_id=?",
            (artifact_id, user_id), fetch="one",
        )
        return Artifact.from_row(row) if row else None

    def list_for_goal(self, user_id: str, goal_id: str) -> List[Artifact]:
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM artifacts WHERE user_id=? AND goal_id=? ORDER BY created_at",
            (user_id, goal_id), fetch="all",
        ) or []
        return [Artifact.from_row(r) for r in rows]

    def list_for_session(self, user_id: str, session_id: str) -> List[Artifact]:
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM artifacts WHERE user_id=? AND session_id=? ORDER BY created_at",
            (user_id, session_id), fetch="all",
        ) or []
        return [Artifact.from_row(r) for r in rows]

    # --- the verdict -----------------------------------------------------

    def verify(self, artifact_id: str, user_id: str) -> str:
        """Is the artifact still on disk, and still what we recorded?

        ``ok`` / ``changed`` / ``missing`` / ``unknown``. This is the call that
        replaces a path-based ``file_contains`` acceptance check: a wipe, a
        rename or a relative-path mismatch all become an honest verdict instead
        of a spurious "the agent never produced it".
        """
        art = self.get(artifact_id, user_id)
        if art is None:
            return VERIFY_UNKNOWN
        try:
            if not os.path.isfile(art.path):
                return VERIFY_MISSING
            sha, _size = hash_file(art.path)
        except OSError:
            return VERIFY_MISSING
        if sha != art.sha256:
            return VERIFY_CHANGED
        execute_retry(
            self.db_path,
            "UPDATE artifacts SET verified_at=? WHERE id=? AND user_id=?",
            (time.time(), artifact_id, user_id),
        )
        return VERIFY_OK


_LEDGER: Optional[ArtifactLedger] = None


def get_artifact_ledger() -> ArtifactLedger:
    """Process-wide ledger on the default DB path."""
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = ArtifactLedger()
    return _LEDGER


def reset_artifact_ledger() -> None:
    """Drop the cached singleton (test isolation / a data-home change)."""
    global _LEDGER
    _LEDGER = None


#: One extension→kind map so every write-time recorder classifies identically.
_KIND_BY_EXT = {
    ".html": KIND_PAGE, ".htm": KIND_PAGE,
    ".py": KIND_CODE, ".js": KIND_CODE, ".ts": KIND_CODE, ".sh": KIND_CODE, ".css": KIND_CODE,
    ".md": KIND_REPORT, ".txt": KIND_REPORT, ".rst": KIND_REPORT,
    ".json": KIND_DATA, ".csv": KIND_DATA, ".yaml": KIND_DATA, ".yml": KIND_DATA,
}


def kind_for_path(path: str) -> str:
    """Classify an artifact by extension (default KIND_FILE)."""
    return _KIND_BY_EXT.get(os.path.splitext(path)[1].lower(), KIND_FILE)


def record_artifact(user_id: Optional[str], path: str, *,
                    session_id: str = "", kind: Optional[str] = None) -> Optional[str]:
    """Tool-agnostic write-time record into the ledger. Fail-open by construction.

    The ledger is the reliable source the acceptance-check + retry-continuity
    paths resolve through, so EVERY tool that produces a workspace file (coding,
    not only the filesystem tool) must record here — otherwise a real deliverable
    reads as "never produced". Never raises: a bookkeeping failure must not break
    a write the agent already completed.

    Returns the artifact's row id (a short uuid4 hex string, ``Artifact.id``) so
    a caller can stamp it onto its own result (e.g.
    ``ActionResult.metadata["artifact_id"]``) for a direct lookup later — or
    ``None`` on any fail-open path (no user_id, the file doesn't exist yet, or
    a bookkeeping error).
    """
    try:
        if not user_id:
            return None
        artifact = get_artifact_ledger().record(
            str(user_id), path, session_id=str(session_id or ""),
            kind=kind or kind_for_path(path))
        return artifact.id if artifact is not None else None
    except Exception:
        logger.debug("artifact record skipped for %s", path, exc_info=True)
        return None
