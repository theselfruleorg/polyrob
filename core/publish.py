"""The ship rail's store: a slug, a served directory, and an owner decision.

Rob could build a page or an API and had no way to give anyone a URL. Every
"ship" goal therefore terminated in a request to the owner: "Ship x402 ecosystem
map: static HTML visualization" completed with a file nobody could reach, and
"Owner deploy package: mainnet-ready x402 endpoint for api.theselfrule.org"
blocked outright. Combined with the workspace cleanup that then deleted the file,
a week of building produced nothing anyone could open.

A publication is (slug -> directory) plus an owner decision:

  pending  staged on disk, NOT served — the owner has not approved this slug yet
  live     served at <base_url>/<slug>/
  (gone)   unpublished; the directory is removed

The owner approves a SLUG once, not every write. That is the difference between
a review gate and a treadmill: the agent must ask before it first puts something
at a public address, then iterates freely at that address.

Slug rules are strict because a slug is BOTH a URL path segment and a directory
name. `valid_slug` is the only way in, and `dir_for` re-checks — a traversal that
reached the filesystem would let a publish write outside the publish root.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)

# Not a valid slug (leading dot), so it can never be requested as one.
_STAGING_DIR = ".pending"

STATUS_PENDING = "pending"
STATUS_LIVE = "live"

MAX_SLUG_LEN = 48
# Lowercase alnum with internal single hyphens. No dots (no `..`, no hidden
# names), no slashes, no underscores, no uppercase — one canonical spelling per
# public address, and nothing that means something special to a path or a URL.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS publications (
    slug        TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    url         TEXT NOT NULL DEFAULT '',
    file_count  INTEGER NOT NULL DEFAULT 0,
    bytes       INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    approved_at REAL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_publications_user ON publications(user_id);
"""


def valid_slug(slug: Any) -> bool:
    """Is *slug* safe as both a URL path segment and a directory name?"""
    if not isinstance(slug, str) or not slug or len(slug) > MAX_SLUG_LEN:
        return False
    return bool(_SLUG_RE.match(slug))


@dataclass
class Publication:
    slug: str
    user_id: str
    status: str
    url: str
    file_count: int
    bytes: int
    created_at: float
    approved_at: Optional[float]
    updated_at: float

    @classmethod
    def from_row(cls, row: Any) -> "Publication":
        d = dict(row)
        return cls(slug=d["slug"], user_id=d["user_id"], status=d["status"],
                   url=d["url"], file_count=int(d["file_count"] or 0),
                   bytes=int(d["bytes"] or 0),
                   created_at=float(d["created_at"] or 0.0),
                   approved_at=d["approved_at"],
                   updated_at=float(d["updated_at"] or 0.0))


def default_publish_db() -> str:
    override = os.getenv("PUBLICATIONS_DB_PATH")
    if override:
        return override
    from core.runtime_config import get_data_root
    return os.path.join(get_data_root(), "publications.db")


def default_publish_root() -> str:
    """Where served files live. Deliberately OUTSIDE the session workspace tree:
    the workspace is scratch that the cleanup may collect, and a published page
    must not vanish because a session aged out."""
    override = os.getenv("PUBLISH_ROOT")
    if override:
        return override
    from core.runtime_config import get_data_root
    return os.path.join(get_data_root(), "publish")


def default_base_url() -> str:
    return (os.getenv("PUBLISH_BASE_URL") or "").rstrip("/")


class PublishStore:
    """Publications + the served directory tree, scoped by tenant."""

    def __init__(self, db_path: Optional[str] = None, root: Optional[str] = None,
                 base_url: Optional[str] = None):
        self.db_path = db_path or default_publish_db()
        self.root = root or default_publish_root()
        self.base_url = (base_url if base_url is not None else default_base_url()).rstrip("/")
        for parent in (os.path.dirname(self.db_path), self.root):
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

    # --- paths -----------------------------------------------------------

    def dir_for(self, slug: str) -> str:
        """The SERVED directory for *slug*. Re-validates: this is the last point
        before a slug becomes a filesystem path."""
        if not valid_slug(slug):
            raise ValueError(f"invalid slug {slug!r}")
        return os.path.join(self.root, slug)

    def staging_dir_for(self, slug: str) -> str:
        """Where a not-yet-approved publication waits.

        Under ``.pending/`` — a leading dot, so it can never be a valid slug and
        therefore can never be requested as one. Keeping unapproved content
        physically OUT of the served tree means the web server needs no
        application logic to enforce the approval gate: a static root plus this
        layout is enough, and a bug in the status column cannot expose a page the
        owner never approved.
        """
        if not valid_slug(slug):
            raise ValueError(f"invalid slug {slug!r}")
        return os.path.join(self.root, _STAGING_DIR, slug)

    def url_for(self, slug: str) -> str:
        if not valid_slug(slug):
            raise ValueError(f"invalid slug {slug!r}")
        return f"{self.base_url}/{slug}/" if self.base_url else f"/{slug}/"

    # --- reads -----------------------------------------------------------

    def get(self, slug: str) -> Optional[Publication]:
        if not valid_slug(slug):
            return None
        row = execute_retry(self.db_path, "SELECT * FROM publications WHERE slug=?",
                            (slug,), fetch="one")
        return Publication.from_row(row) if row else None

    def list_for(self, user_id: str) -> List[Publication]:
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM publications WHERE user_id=? ORDER BY created_at",
            (user_id,), fetch="all") or []
        return [Publication.from_row(r) for r in rows]

    def is_live(self, slug: str) -> bool:
        pub = self.get(slug)
        return bool(pub and pub.status == STATUS_LIVE)

    # --- writes ----------------------------------------------------------

    def stage(self, user_id: str, slug: str, sources: List[str], *,
              confine_to: Optional[str] = None) -> Publication:
        """Copy *sources* into the slug's directory and record the publication.

        A NEW slug lands ``pending`` — not served until the owner approves it. An
        already-approved slug stays ``live``, so the agent can iterate on a page
        it has already been granted an address for.

        ``confine_to`` restricts what may be copied to a directory subtree
        (the caller passes the session workspace). Without it a publish could
        copy any readable file — including a credential — to a public URL.
        """
        if not valid_slug(slug):
            raise ValueError(f"invalid slug {slug!r}")
        existing = self.get(slug)
        if existing is not None and existing.user_id != user_id:
            raise PermissionError(f"slug {slug!r} belongs to another tenant")

        resolved = [self._vet_source(src, confine_to) for src in (sources or [])]
        if not resolved:
            raise ValueError("nothing to publish")

        # An approved slug writes straight into the served tree; an unapproved one
        # waits in staging and only MOVES there on approval.
        already_live = existing is not None and existing.status == STATUS_LIVE
        target = self.dir_for(slug) if already_live else self.staging_dir_for(slug)
        # Replace wholesale: a publication is the CURRENT state of a page, and a
        # stale leftover file from an earlier version staying reachable at a
        # public URL is its own kind of dishonesty.
        if os.path.isdir(target):
            shutil.rmtree(target)
        os.makedirs(target, exist_ok=True)

        total = 0
        for src in resolved:
            dest = os.path.join(target, os.path.basename(src))
            shutil.copy2(src, dest)
            total += os.path.getsize(dest)

        now = time.time()
        status = existing.status if existing is not None else STATUS_PENDING
        url = self.url_for(slug)
        if existing is None:
            execute_retry(
                self.db_path,
                "INSERT INTO publications (slug, user_id, status, url, file_count, "
                "bytes, created_at, approved_at, updated_at) VALUES (?,?,?,?,?,?,?,NULL,?)",
                (slug, user_id, status, url, len(resolved), total, now, now))
        else:
            execute_retry(
                self.db_path,
                "UPDATE publications SET file_count=?, bytes=?, url=?, updated_at=? "
                "WHERE slug=? AND user_id=?",
                (len(resolved), total, url, now, slug, user_id))
        return self.get(slug)

    def approve(self, user_id: str, slug: str) -> bool:
        """Owner decision: this slug may be served. Tenant-scoped CAS."""
        if not valid_slug(slug):
            return False
        rc = execute_retry(
            self.db_path,
            "UPDATE publications SET status=?, approved_at=?, updated_at=? "
            "WHERE slug=? AND user_id=? AND status=?",
            (STATUS_LIVE, time.time(), time.time(), slug, user_id, STATUS_PENDING))
        if rc != 1:
            return False
        # Promote the staged directory INTO the served tree. The status flip and
        # this move are the same decision; doing the move only after the CAS wins
        # means a losing racer never publishes files.
        staged, served = self.staging_dir_for(slug), self.dir_for(slug)
        try:
            if os.path.isdir(served):
                shutil.rmtree(served)
            if os.path.isdir(staged):
                os.replace(staged, served)
        except OSError:
            logger.exception("publish: promoting %s failed; rolling status back", slug)
            execute_retry(
                self.db_path,
                "UPDATE publications SET status=?, approved_at=NULL WHERE slug=? AND user_id=?",
                (STATUS_PENDING, slug, user_id))
            return False
        return True

    def unpublish(self, user_id: str, slug: str) -> bool:
        """Take it down and remove the files. Tenant-scoped."""
        if not valid_slug(slug):
            return False
        pub = self.get(slug)
        if pub is None or pub.user_id != user_id:
            return False
        rc = execute_retry(self.db_path,
                           "DELETE FROM publications WHERE slug=? AND user_id=?",
                           (slug, user_id))
        for target in (self.dir_for(slug), self.staging_dir_for(slug)):
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
        return rc == 1

    # --- internals -------------------------------------------------------

    @staticmethod
    def _vet_source(src: Any, confine_to: Optional[str]) -> str:
        """Resolve *src* to a real file that is safe to copy to a PUBLIC URL."""
        path = os.path.realpath(str(src))
        if not os.path.isfile(path):
            raise ValueError(f"not a file: {src!r}")
        if confine_to:
            base = os.path.realpath(confine_to)
            if not (path == base or path.startswith(base + os.sep)):
                raise ValueError(f"refusing to publish a file outside {confine_to!r}: {src!r}")
        # Never let a credential reach a public address, even from inside the
        # workspace — the confinement floor cannot catch a secret that legitimately
        # lives there (see core/security/secret_guard.py).
        from pathlib import Path

        from core.security.secret_guard import is_credential_file
        if is_credential_file(Path(path)):
            raise ValueError(f"refusing to publish a credential file: {os.path.basename(path)}")
        return path


_STORE: Optional[PublishStore] = None


def get_publish_store() -> PublishStore:
    global _STORE
    if _STORE is None:
        _STORE = PublishStore()
    return _STORE


def reset_publish_store() -> None:
    global _STORE
    _STORE = None
