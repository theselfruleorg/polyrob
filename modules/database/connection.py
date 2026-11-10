import aiosqlite
import asyncio
import contextlib
import logging
from typing import Optional, List, Any, Dict, Union, Tuple
import json
from datetime import datetime
import sqlite3
import os
from pathlib import Path

from core.data_perms import apply_birth_mode
from core.exceptions import DatabaseError
from core.sqlite_util import _WAL_SIZE_LIMIT_BYTES

#: How long one statement waits on another process's write lock before
#: "database is locked" (ms). Explicit, not the sqlite3 module default: the
#: agent, console, API and email units share one bot.db.
BUSY_TIMEOUT_MS = 5000


class DatabaseConnection:
    """Manages SQLite database connection."""
    
    def __init__(self, db_path: Path, logger: Optional[logging.Logger] = None):
        """Initialize database connection.
        
        Args:
            db_path: Path to SQLite database file
            logger: Optional logger instance
        """
        self.db_path = db_path
        self.logger = logger or logging.getLogger(__name__)
        self.connection: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()
        self._transaction_lock = asyncio.Lock()
        self._in_transaction = False
        # H8 (shared-connection transaction ownership): a transaction span OWNS
        # the single shared sqlite3 connection. `begin_transaction` records the
        # owning asyncio task here; while a transaction is open, an `execute()`
        # from a DIFFERENT task WAITS on `_txn_done` for that transaction to
        # finish instead of silently joining it (a joined write's auto-commit is
        # skipped and can then be destroyed by the owner's ROLLBACK — the H8
        # fund-loss bug). `_txn_done` is SET whenever no transaction is open and
        # CLEARED for the span of one.
        self._txn_owner: Optional["asyncio.Task"] = None
        self._txn_done = asyncio.Event()
        self._txn_done.set()
        
    async def connect(self) -> None:
        """Establish database connection."""
        try:
            self._birth_private()
            self.connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=BUSY_TIMEOUT_MS / 1000,
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
            self._enable_wal()

            # SECURITY FIX: Enable foreign key constraints for data integrity
            # FK constraints ensure referential integrity and prevent orphaned records
            # Schema uses ON DELETE CASCADE to handle user deletions properly
            self.connection.execute("PRAGMA foreign_keys = ON")

            # Clean up orphaned records that might have accumulated when FK was disabled
            await self._cleanup_orphaned_records()

            self.logger.debug(f"Connected to database at {self.db_path} (FK: ON)")
            
        except Exception as e:
            self.logger.error(f"Failed to connect to database: {e}")
            raise DatabaseError(f"Failed to connect to database: {e}")
            
    def _birth_private(self) -> None:
        """Create a missing bot.db before SQLite does, under the shared birth rule.

        SQLite births a file 0644 whatever the umask says, and -wal/-shm copy
        that mode. Create it 0600 with ``O_EXCL`` (as ``wal_connect`` births a
        sidecar store), then ``core.data_perms.apply_birth_mode`` gives the
        group read-write, never other users; a setgid data home gives the
        shared group. An existing file's mode is the operator's and is left.
        """
        if str(self.db_path) == ":memory:":
            return
        try:
            fd = os.open(self.db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            return
        os.close(fd)
        apply_birth_mode(self.db_path)

    def _enable_wal(self) -> None:
        """WAL: readers and the one writer no longer block each other (the agent,
        the console and the API share this file). The mode is stored in the file;
        a switch that cannot take its lock now keeps the old mode and the next
        connect tries again. Backups stay correct: the update snapshot and
        scripts/backup_database.sh copy through SQLite's own backup.

        Not ``core.sqlite_util.wal_connect``: that one falls back to DELETE on
        any refusal and opens a thread-bound handle; this connection keeps its
        mode on a locked switch and is shared across threads. The -wal size
        limit is the shared one, so the file shrinks after a checkpoint."""
        if str(self.db_path) == ":memory:":
            return
        try:
            mode = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                self.logger.warning("bot.db stays in journal mode %s (WAL refused)", mode)
        except sqlite3.OperationalError as e:
            self.logger.warning("bot.db WAL switch skipped this start: %s", e)
        self.connection.execute(
            f"PRAGMA journal_size_limit={int(_WAL_SIZE_LIMIT_BYTES)}")

    async def close(self) -> None:
        """Close database connection."""
        if self.connection:
            self.connection.close()
            self.connection = None
            self.logger.debug("Database connection closed")

    async def _cleanup_orphaned_records(self) -> None:
        """Clean up orphaned records that accumulated while FK constraints were disabled.

        This runs once at connection time to fix any data integrity issues
        before FK constraints are enforced.
        """
        try:
            # Temporarily disable FK for cleanup (we're fixing old data)
            self.connection.execute("PRAGMA foreign_keys = OFF")

            # Tables with FK to user_profiles(user_id)
            orphan_tables = [
                'api_keys',
                'user_credits',
                'credit_transactions',
                'usage_records',
                'user_deposit_addresses',
                'crypto_payments',
                'pending_sweeps',
                'wallet_history',
                'blocked_users',
                'conversation_contexts',
            ]

            total_cleaned = 0
            for table in orphan_tables:
                try:
                    # Check if table exists before cleaning
                    cursor = self.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)
                    )
                    if cursor.fetchone() is None:
                        continue

                    # Delete orphaned records
                    cursor = self.connection.execute(f"""
                        DELETE FROM {table}
                        WHERE user_id IS NOT NULL
                        AND user_id NOT IN (SELECT user_id FROM user_profiles)
                    """)
                    if cursor.rowcount > 0:
                        total_cleaned += cursor.rowcount
                        self.logger.info(f"Cleaned {cursor.rowcount} orphaned records from {table}")
                except Exception as e:
                    # Log but continue - some tables may not exist yet
                    self.logger.debug(f"Could not clean {table}: {e}")

            # Clean up expired nonces
            try:
                cursor = self.connection.execute("""
                    DELETE FROM auth_nonces WHERE expires_at < datetime('now')
                """)
                if cursor.rowcount > 0:
                    self.logger.info(f"Cleaned {cursor.rowcount} expired nonces")
            except Exception:
                pass  # Table may not exist

            self.connection.commit()

            # Re-enable FK constraints
            self.connection.execute("PRAGMA foreign_keys = ON")

            if total_cleaned > 0:
                self.logger.info(f"✅ Cleaned up {total_cleaned} total orphaned records")

        except Exception as e:
            self.logger.warning(f"Error during orphan cleanup: {e}")
            # Re-enable FK even if cleanup failed
            self.connection.execute("PRAGMA foreign_keys = ON")

    # NOTE (U11, 2026-07-14 review): the legacy inline-schema initializers
    # (_initialize_base_schema / _ensure_required_columns /
    # _initialize_remaining_schema / _split_sql_statements) were DELETED — they
    # had zero callers, and _initialize_base_schema created the dead singular
    # `schema_version` table (the SSOT is `schema_versions`, plural, owned by
    # migrations/version_manager.py). The live inline schema is created by the
    # component table classes (auth_tables/x402_tables/user_profiles/...);
    # tests/unit/migrations/test_inline_schema_at_head.py enforces that it
    # matches migration HEAD. (The unloaded schema.sql "mirror" was deleted
    # 2026-10-10, W8 — a fourth schema source nothing read.)

    async def begin_transaction(self) -> None:
        """Begin a new transaction.

        SECURITY FIX: Set _in_transaction flag BEFORE calling execute() to prevent
        race condition where another coroutine could auto-commit during the window
        between execute() returning and flag being set.
        """
        async with self._transaction_lock:
            if self._in_transaction:
                raise DatabaseError("Transaction already in progress")
            # Set flag BEFORE execute to close race window. Record the owner task
            # (H8): only this task's statements may proceed while the span is open;
            # other tasks wait on `_txn_done` (cleared here) rather than joining.
            self._in_transaction = True
            self._txn_owner = asyncio.current_task()
            self._txn_done.clear()
            if self._txn_owner is not None:
                # A waiter already parked on this span must not depend on a NEW
                # caller to clear a dead owner (at shutdown none comes): the
                # owner's end wakes the waiters, and the woken one rolls back.
                self._txn_owner.add_done_callback(self._wake_on_owner_end)
            try:
                # IMMEDIATE: every span writes, and under WAL a DEFERRED span
                # that read first fails at once (BUSY_SNAPSHOT, no busy wait)
                # when another process commits before its first write.
                await self.execute("BEGIN IMMEDIATE TRANSACTION")
            except Exception:
                # Rollback state on failure — and release any waiters.
                self._end_span()
                raise

    async def commit(self) -> None:
        """Commit the current transaction."""
        async with self._transaction_lock:
            if not self._in_transaction:
                raise DatabaseError("No transaction in progress")
            try:
                await self.execute("COMMIT")
            finally:
                # Always clear flag + release waiters, even on error (the
                # transaction is no longer active either way).
                self._end_span()

    async def rollback(self) -> None:
        """Rollback the current transaction."""
        async with self._transaction_lock:
            if not self._in_transaction:
                return  # Silently ignore if no transaction
            try:
                await self.execute("ROLLBACK")
            finally:
                # Always clear flag + release waiters, even on error
                self._end_span()

    def _end_span(self) -> None:
        """Close the span's Python-side state and release every waiter."""
        owner, self._txn_owner = self._txn_owner, None
        if owner is not None:
            owner.remove_done_callback(self._wake_on_owner_end)
        self._in_transaction = False
        self._txn_done.set()

    def _wake_on_owner_end(self, task: "asyncio.Task") -> None:
        """Done-callback of a span owner: wake the waiters if it ended inside its span.

        Only wakes; the woken waiter sees ``owner.done()`` in
        ``_foreign_span_waiter`` and runs the rollback-and-clear path there.
        """
        if self._in_transaction and self._txn_owner is task:
            self._txn_done.set()

    async def in_transaction(self) -> bool:
        """Check if currently in a transaction."""
        return self._in_transaction

    def _foreign_span_waiter(self) -> Optional[asyncio.Event]:
        """The event to wait on when ANOTHER task owns the open transaction, else None.

        Called with ``self._lock`` held. Every statement — a read too (EP-4: a
        read used to see another task's uncommitted span, and ``executemany``
        committed it) — waits for a foreign span; the owning task proceeds.

        A span body must never read or write through a child task,
        ``asyncio.gather`` or ``asyncio.wait_for``: the child is a DIFFERENT
        task, so it waits for the span its own parent holds, and the parent
        waits for the child — for ever. (On Python 3.11 ``wait_for`` wraps the
        awaited call in a new task.) Await the statement directly.
        """
        waiter: Optional[asyncio.Event] = None
        if (self._in_transaction
                and self._txn_owner is not None
                and asyncio.current_task() is not self._txn_owner):
            # A DIFFERENT task owns the open transaction.
            if self._txn_owner.done():
                # Owner task ended without commit/rollback (e.g. an
                # unhandled cancellation between begin and its first
                # statement). Never wait forever on an abandoned
                # transaction — reset the span and proceed.
                self.logger.warning(
                    "Transaction owner task ended without commit/"
                    "rollback — clearing abandoned transaction state "
                    "to avoid a stuck shared connection")
                # H8 fix: the dead owner may have left a PARTIAL
                # write open at the sqlite level (e.g. a balance
                # UPDATE without its paired ledger INSERT). If we
                # only clear the Python-side flags below, the next
                # non-owner execute() sees `_in_transaction=False`
                # and auto-commits — which commits the ENTIRE
                # pending sqlite transaction, silently landing the
                # dead task's partial span too (money fails OPEN).
                # Discard that span BEFORE clearing state. This is
                # a direct call on the raw sqlite3.Connection
                # (self.connection), NOT the async self.rollback()
                # wrapper — we already hold `self._lock` here and
                # self.rollback() acquires `_transaction_lock` /
                # re-enters execute(), which would deadlock.
                if self.connection is not None:
                    try:
                        self.connection.rollback()
                    except Exception as rollback_exc:
                        # A failed rollback must not deadlock every
                        # other waiter on this connection — log
                        # and still clear state below so the
                        # escape hatch keeps its no-deadlock
                        # guarantee.
                        self.logger.error(
                            "Failed to roll back abandoned "
                            "transaction's sqlite-level span: %s",
                            rollback_exc)
                self._in_transaction = False
                self._txn_owner = None
                self._txn_done.set()
            else:
                waiter = self._txn_done
        return waiter

    @contextlib.asynccontextmanager
    async def _unshared(self):
        """Hold ``self._lock`` on a connected handle with no FOREIGN transaction open."""
        while True:
            await self._lock.acquire()
            try:
                if not self.connection:
                    await self.connect()
                waiter = self._foreign_span_waiter()
            except BaseException:
                self._lock.release()
                raise
            if waiter is None:
                try:
                    yield
                finally:
                    self._lock.release()
                return
            self._lock.release()
            await waiter.wait()

    async def execute(self, query: str, params: Union[tuple, Dict[str, Any]] = ()) -> aiosqlite.Cursor:
        """Execute SQL query.

        H8 (shared-connection transaction ownership): while a transaction is
        open, an ``execute()`` from a task OTHER than the one that called
        ``begin_transaction`` WAITS for that transaction to complete instead of
        silently joining it. Statements from the owner task — and BEGIN/COMMIT/
        ROLLBACK control statements — proceed immediately. Public API unchanged.
        """
        # Transaction-control statements (issued only by begin/commit/rollback,
        # which already hold `_transaction_lock`) must never be gated — they are
        # what ends the wait.
        query_upper = query.strip().upper()
        is_transaction_control = query_upper.startswith(('BEGIN', 'COMMIT', 'ROLLBACK'))
        try:
            while True:
                waiter: Optional[asyncio.Event] = None
                async with self._lock:
                    if not self.connection:
                        await self.connect()

                    waiter = None if is_transaction_control else self._foreign_span_waiter()
                    must_wait = waiter is not None

                    if not must_wait:
                        # Debug logging
                        self.logger.debug(f"Execute query: {query}")
                        self.logger.debug(f"Params before processing: {params}")

                        # Handle JSON serialization in parameters
                        if isinstance(params, dict):
                            processed_params = {k: json.dumps(v) if isinstance(v, (dict, list)) else v
                                             for k, v in params.items()}
                        else:
                            processed_params = tuple(json.dumps(p) if isinstance(p, (dict, list)) else p
                                                 for p in params)

                        # Debug logging
                        self.logger.debug(f"Processed params: {processed_params}")

                        cursor = self.connection.execute(query, processed_params)

                        # Only auto-commit if NOT in an explicit transaction AND
                        # not a transaction control statement.
                        if not self._in_transaction and not is_transaction_control:
                            self.connection.commit()

                        return cursor
                # Released the lock: wait for the owning transaction to finish,
                # then retry the ownership check (another transaction may have
                # started, or this one may have ended).
                await waiter.wait()
        except Exception as e:
            self.logger.error(f"Execute error: {e} | Query: {query} | Params: {params}")
            self.logger.exception("Detailed error:")
            raise

    async def fetch_one(self, query: str, params: Union[tuple, Dict[str, Any]] = ()) -> Optional[Dict[str, Any]]:
        """Fetch a single row from the database."""
        try:
            async with self._unshared():
                    
                # Debug logging
                self.logger.debug(f"Fetch one query: {query}")
                self.logger.debug(f"Params before processing: {params}")
                    
                # Handle JSON serialization in parameters
                if isinstance(params, dict):
                    processed_params = {k: json.dumps(v) if isinstance(v, (dict, list)) else v 
                                     for k, v in params.items()}
                else:
                    processed_params = tuple(json.dumps(p) if isinstance(p, (dict, list)) else p 
                                         for p in params)
                    
                # Debug logging    
                self.logger.debug(f"Processed params: {processed_params}")
                    
                cursor = self.connection.execute(query, processed_params)
                row = cursor.fetchone()
                cursor.close()
                
                if row:
                    columns = [description[0] for description in cursor.description]
                    # Handle potential JSON fields
                    result = {}
                    for col, val in zip(columns, row):
                        try:
                            if val is not None and isinstance(val, str) and col in ('preferences', 'metadata', 'messages', 'mode_metadata', 'verification_status'):
                                try:
                                    result[col] = json.loads(val)
                                except json.JSONDecodeError:
                                    result[col] = val
                            else:
                                # For NULL JSON fields, use empty dict/list based on field name
                                if val is None and col in ('preferences', 'metadata', 'messages', 'mode_metadata', 'verification_status'):
                                    if col == 'messages':
                                        result[col] = []
                                    elif col == 'verification_status':
                                        result[col] = {"email": False, "phone": False}
                                    else:
                                        result[col] = {}
                                else:
                                    result[col] = val
                        except Exception as e:
                            self.logger.error(f"Error processing column {col} with value {val}: {e}")
                            result[col] = val
                    return result
                return None
        except Exception as e:
            self.logger.error(f"Fetch one error: {e} | Query: {query} | Params: {params}")
            self.logger.exception("Detailed error:")
            raise

    async def fetch_all(self, query: str, params: Union[tuple, Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
        """Fetch all rows and return as list of dictionaries."""
        try:
            async with self._unshared():
                    
                # Debug logging
                self.logger.debug(f"Fetch all query: {query}")
                self.logger.debug(f"Params before processing: {params}")
                    
                # Handle JSON serialization in parameters
                if isinstance(params, dict):
                    processed_params = {k: json.dumps(v) if isinstance(v, (dict, list)) else v 
                                     for k, v in params.items()}
                else:
                    processed_params = tuple(json.dumps(p) if isinstance(p, (dict, list)) else p 
                                         for p in params)
                
                # Debug logging    
                self.logger.debug(f"Processed params: {processed_params}")
                    
                cursor = self.connection.execute(query, processed_params)
                rows = cursor.fetchall()
                columns = [description[0] for description in cursor.description]
                cursor.close()
                
                # Debug logging
                self.logger.debug(f"Raw rows: {rows}")
                self.logger.debug(f"Columns: {columns}")
                
                # Convert rows to dicts and handle JSON fields
                result = []
                for row in rows:
                    row_dict = {}
                    for col, val in zip(columns, row):
                        try:
                            # Handle potential JSON fields
                            if val is not None and isinstance(val, str) and col in ('preferences', 'metadata', 'messages', 'mode_metadata', 'verification_status'):
                                try:
                                    row_dict[col] = json.loads(val)
                                except json.JSONDecodeError:
                                    row_dict[col] = val
                            else:
                                # For NULL JSON fields, use empty dict/list based on field name
                                if val is None and col in ('preferences', 'metadata', 'messages', 'mode_metadata', 'verification_status'):
                                    if col == 'messages':
                                        row_dict[col] = []
                                    elif col == 'verification_status':
                                        row_dict[col] = {"email": False, "phone": False}
                                    else:
                                        row_dict[col] = {}
                                else:
                                    row_dict[col] = val
                        except Exception as e:
                            self.logger.error(f"Error processing column {col} with value {val}: {e}")
                            row_dict[col] = val
                    result.append(row_dict)
                
                # Debug logging
                self.logger.debug(f"Processed result: {result}")
                
                return result
        except Exception as e:
            # Raise like fetch_one: "could not read" (a locked or broken store)
            # must never read as "no rows" (EP-3) — a reaper skipped its work and
            # a PRAGMA probe answered "table missing" on a locked bot.db.
            self.logger.error(f"Fetch all error: {e} | Query: {query} | Params: {params}")
            self.logger.exception("Detailed error:")
            raise

    async def executemany(self, query: str, param_list: List[Union[tuple, Dict[str, Any]]]) -> None:
        """Execute multiple queries with parameter lists."""
        try:
            async with self._unshared():
                    
                # Handle JSON serialization in parameters
                processed_params = []
                for params in param_list:
                    if isinstance(params, dict):
                        processed = {k: json.dumps(v) if isinstance(v, (dict, list)) else v 
                                   for k, v in params.items()}
                    else:
                        processed = tuple(json.dumps(p) if isinstance(p, (dict, list)) else p 
                                       for p in params)
                    processed_params.append(processed)
                
                self.connection.executemany(query, processed_params)
                if not self._in_transaction:  # EP-4: never commit an open span
                    self.connection.commit()
                self.logger.debug(f"Executed multiple queries: {query}")
        except Exception as e:
            self.logger.error(f"Executemany error: {e} | Query: {query}")
            raise

    async def __aenter__(self):
        """Async context manager entry."""
        if not self.connection:
            await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if exc_type:
            self.connection.rollback()
        else:
            self.connection.commit()
        await self.close()

    async def transaction(self, statements: List[Tuple[str, tuple]]) -> None:
        """Execute multiple statements in a transaction.
        
        Args:
            statements: List of (query, params) tuples to execute
        """
        try:
            async with self._transaction_lock:
                if not self.connection:
                    await self.connect()
                
                # Start transaction
                self.connection.execute("BEGIN TRANSACTION")
                
                try:
                    # Execute all statements
                    for query, params in statements:
                        self.connection.execute(query, params)
                    
                    # Commit if all successful
                    self.connection.commit()
                    
                except Exception as e:
                    # Rollback on any error
                    self.connection.rollback()
                    self.logger.error(f"Transaction failed, rolling back: {e}")
                    raise DatabaseError(f"Transaction failed: {e}")
                    
        except Exception as e:
            self.logger.error(f"Error executing transaction: {e}")
            raise DatabaseError(f"Failed to execute transaction: {e}")