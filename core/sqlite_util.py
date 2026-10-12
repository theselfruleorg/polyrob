"""Small shared SQLite helper: WAL connection + jittered write-retry (roadmap P6).

Centralizes the WAL-with-DELETE-fallback + 'database is locked' retry pattern so
the cross-process session registry (and, later, the cron store) don't each
re-implement it. Mirrors Reference ``SessionDB``: WAL for concurrent readers + one
writer, DELETE fallback on NFS/SMB/FUSE, and 20–150ms jitter to avoid SQLite's
deterministic backoff convoy under contention.
"""
from __future__ import annotations

import random
import os
import stat
import sqlite3
import time
from typing import Optional

_MAX_RETRIES = 15
_BUSY_TIMEOUT_S = 1.0


def wal_connect(db_path: str, timeout: float = _BUSY_TIMEOUT_S) -> sqlite3.Connection:
    if str(db_path) != ':memory:':
        # Create private BEFORE SQLite can write sensitive rows or sidecars.
        # Refuse existing file aliases; chmod after connect would follow them.
        try:
            fd = os.open(
                db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, 'O_NOFOLLOW', 0), 0o600,
            )
        except FileExistsError:
            info = os.lstat(db_path)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError('SQLite store must be a single-link regular file')
        else:
            os.close(fd)
            from core.data_perms import apply_birth_mode
            apply_birth_mode(db_path)
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                info = os.lstat(str(db_path) + suffix)
            except FileNotFoundError:
                continue
            # nlink 0: another UID's last connection is unlinking it right now.
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise OSError('SQLite sidecar must be a single-link regular file')
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        conn.execute("PRAGMA journal_mode=DELETE")
    return conn


def init_schema(db_path: str, script: str, *, mkdir: bool = False,
                version: Optional[int] = None,
                columns: Optional[dict] = None) -> None:
    """Run a ``CREATE … IF NOT EXISTS`` script once over a WAL connection.

    The open/executescript/commit/close four-liner every sidecar store's
    ``_init_schema`` carried by hand. ``mkdir=True`` creates the parent
    directory first (the fail-open stores that keep a ``_ready`` flag).
    Raises on failure; the caller decides whether that is fatal.

    *columns* (``{table: ((name, decl), …)}``) adds the columns a pre-existing
    table lacks (:func:`ensure_columns`) — the additive migration a
    ``CREATE IF NOT EXISTS`` never performs. *version* raises ``PRAGMA
    user_version`` to that number (never lowers it). It is opt-in: some stores
    already use ``user_version`` as their own one-shot-migration marker
    (correspondents, conversations, dead_targets), and a stamp from here would
    make them skip that migration.
    """
    import os
    if mkdir:
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    fresh = not os.path.exists(db_path)
    conn = wal_connect(db_path)
    try:
        conn.executescript(script)
        for table, cols in (columns or {}).items():
            ensure_columns(conn, table, cols)
        if version is not None:
            set_user_version(conn, version)
        conn.commit()
    finally:
        conn.close()
    if fresh:
        # SQLite creates a db 0644 whatever the process umask says, and the WAL
        # `-wal`/`-shm` files copy that mode. The three service identities share
        # the data home through the `polyrob-data` group, so a store born between
        # deploys (prod 2026-09-20 03:31Z: 057's verdicts.db, thirty seconds AFTER
        # the deploy's chmod g+rw pass) is unwritable by its siblings until the
        # next deploy repairs it — "attempt to write a readonly database" in a
        # unit that did nothing wrong. Apply the deploy's rule at birth (the ONE
        # rule, `core.data_perms.apply_birth_mode`). An existing file's mode is
        # an operator's and is left.
        from core.data_perms import apply_birth_mode
        apply_birth_mode(db_path)


def ensure_columns(conn: sqlite3.Connection, table: str, columns) -> list:
    """Add each ``(name, decl)`` column *table* lacks; return the names added.

    The ONE idempotent additive migration (W8): read ``PRAGMA table_info``,
    ``ALTER TABLE … ADD COLUMN`` only what is missing, and treat a concurrent
    process's duplicate-column error as already-migrated (the only error
    swallowed). The caller owns the transaction/commit.
    """
    _ident(table)
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    added = []
    for name, decl in columns:
        _ident(name)
        if name in have:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
            continue
        have.add(name)
        added.append(name)
    return added


def set_user_version(conn: sqlite3.Connection, version: int) -> int:
    """Raise ``PRAGMA user_version`` to *version* (never lower it); return it."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    target = int(version)
    if target > current:
        conn.execute(f"PRAGMA user_version = {target}")
        return target
    return current


def _ident(name: str) -> None:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"not a plain SQL identifier: {name!r}")


def execute_retry(db_path: str, sql: str, params: tuple = (), *, fetch: Optional[str] = None):
    """Execute one statement with jittered retry on write contention.

    fetch: None -> rowcount; 'one' -> a Row or None; 'all' -> list[Row];
    'lastrowid' -> the INSERT's rowid (for sidecar tables keyed by it).
    """
    last_err: Optional[Exception] = None
    for _ in range(_MAX_RETRIES):
        try:
            conn = wal_connect(db_path)
            try:
                cur = conn.execute(sql, params)
                if fetch == "one":
                    row = cur.fetchone()
                    conn.commit()
                    return row
                if fetch == "all":
                    rows = cur.fetchall()
                    conn.commit()
                    return rows
                if fetch == "lastrowid":
                    rowid = cur.lastrowid
                    conn.commit()
                    return rowid
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                last_err = e
                time.sleep(random.uniform(0.02, 0.15))
                continue
            raise
    raise last_err  # type: ignore[misc]
