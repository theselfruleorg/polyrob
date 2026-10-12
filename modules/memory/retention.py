"""Memory-store hygiene on the curator tick (B2, 2026-10-11).

Two jobs, both server-side, both fail-open:

* :func:`autonomous_window_days` — the shorter age window for rows an
  autonomous (cron/goal) run wrote. Prod wrote 250–1,200 such rows a day and
  the 365-day window never deleted one; owner chat rows keep the long window.
* :func:`purge_noise_rows` — the one-time cleanup of rows already stored that
  the write filter (:mod:`modules.memory.write_filter`) would now refuse:
  ``[STRATEGIC CLUE FROM …]`` lines, ``tool(args)→result`` echoes and
  run-status narration. A row that is ALL noise is deleted; a row that also
  holds a finding is rewritten to the finding. It runs once per
  ``CLASSIFIER_VERSION`` (a marker row in ``mem_housekeeping``), in batches,
  and the FTS index stays consistent because ``memories`` is a plain FTS5
  table (DELETE/UPDATE maintain the index).
"""
import hashlib
import logging
import os
import sqlite3
import time
from typing import Dict

from core.sqlite_util import wal_connect
from modules.memory import write_filter

logger = logging.getLogger(__name__)

#: Rows an autonomous run wrote age out after this many days (capped by
#: MEMORY_RETENTION_DAYS when that is shorter) — unless the row carries an owner
#: ruling, a lesson or a decision (``write_filter.is_protected``): those keep
#: the long window. Not a flag on purpose.
AUTONOMOUS_RETENTION_DAYS = 45

_BATCH = 500
_MARKER = "noise_purge_version"


def autonomous_window_days(general_days: int) -> int:
    """The autonomous-row window for a general window of *general_days*."""
    return max(1, min(AUTONOMOUS_RETENTION_DAYS, int(general_days)))


def _ensure_marker_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS mem_housekeeping ("
                 "key TEXT PRIMARY KEY, value TEXT, ts INTEGER)")


def noise_purge_done(db_path: str) -> bool:
    conn = wal_connect(db_path)
    try:
        _ensure_marker_table(conn)
        row = conn.execute("SELECT value FROM mem_housekeeping WHERE key = ?",
                           (_MARKER,)).fetchone()
        return row is not None and str(row[0]) == str(write_filter.CLASSIFIER_VERSION)
    finally:
        conn.close()


def backup_path(db_path: str) -> str:
    return f"{db_path}.bak-purge-v{write_filter.CLASSIFIER_VERSION}"


def backup_before_purge(db_path: str) -> bool:
    """One copy of the store per classifier version, taken with the SQLite
    online-backup API (consistent under WAL). An existing copy is kept — a
    resumed purge must not overwrite the pre-purge state with a half-purged
    one. False when the copy cannot be made: the caller skips the purge."""
    dest = backup_path(db_path)
    if os.path.exists(dest):
        return True
    tmp = dest + ".tmp"
    try:
        src = sqlite3.connect(db_path)
        try:
            out = sqlite3.connect(tmp)
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        os.replace(tmp, dest)
        logger.info("memory noise purge: backup written to %s", dest)
        return True
    except Exception as e:
        logger.warning("memory noise purge SKIPPED: backup to %s failed: %s", dest, e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def purge_noise_rows(provider, *, force: bool = False) -> Dict[str, int]:
    """Delete or rewrite stored rows the write filter now refuses.

    Returns ``{"scanned", "deleted", "rewritten"}`` (``"skipped": 1`` when the
    marker says this classifier version already ran, plus ``"backup_failed": 1``
    when the pre-purge copy could not be written — nothing is touched then).
    The first pass writes ``<db>.bak-purge-v<CLASSIFIER_VERSION>``. Each batch is one
    transaction over ``memories`` + ``mem_provenance``; a crash mid-way leaves
    whole batches done and the marker unset, so the next tick resumes (the
    classifier is idempotent — a cleaned row is not noise any more).
    """
    db_path = getattr(provider, "db_path", "")
    out = {"scanned": 0, "deleted": 0, "rewritten": 0}
    if not db_path:
        return out
    if not force and noise_purge_done(db_path):
        out["skipped"] = 1
        return out
    if not backup_before_purge(db_path):
        out["skipped"] = 1
        out["backup_failed"] = 1
        return out
    vec_doomed = []
    last = 0
    while True:
        conn = wal_connect(db_path)
        try:
            rows = conn.execute(
                "SELECT rowid, user_id, content FROM memories WHERE rowid > ? "
                "ORDER BY rowid LIMIT ?", (last, _BATCH)).fetchall()
            if not rows:
                break
            last = rows[-1][0]
            with conn:
                for rowid, user_id, content in rows:
                    out["scanned"] += 1
                    kept, reasons = write_filter.filter_content(content or "")
                    if not reasons:
                        continue
                    vec_doomed.append((user_id, content))
                    if not kept:
                        conn.execute("DELETE FROM memories WHERE rowid = ?", (rowid,))
                        conn.execute("DELETE FROM mem_provenance WHERE mem_rowid = ?",
                                     (rowid,))
                        out["deleted"] += 1
                        continue
                    conn.execute("UPDATE memories SET content = ? WHERE rowid = ?",
                                 (kept, rowid))
                    conn.execute(
                        "UPDATE mem_provenance SET content_hash = ? WHERE mem_rowid = ?",
                        (hashlib.sha256(kept.encode("utf-8", "replace")).hexdigest(),
                         rowid))
                    out["rewritten"] += 1
        finally:
            conn.close()
    conn = wal_connect(db_path)
    try:
        with conn:
            _ensure_marker_table(conn)
            conn.execute("INSERT OR REPLACE INTO mem_housekeeping (key, value, ts) "
                         "VALUES (?, ?, ?)",
                         (_MARKER, str(write_filter.CLASSIFIER_VERSION), int(time.time())))
    finally:
        conn.close()
    # The vector twins (local_vector backend) were embedded from the OLD text;
    # drop them — the keyword row keeps serving recall. Fail-open.
    if vec_doomed and getattr(provider, "_vec_ok", False):
        try:
            provider._vec_prune(vec_doomed)
        except Exception as e:
            logger.debug("noise purge: vector twin prune skipped: %s", e)
    return out
