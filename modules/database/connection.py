import aiosqlite
import asyncio
import logging
from typing import Optional, List, Any, Dict, Union, Tuple
import json
from datetime import datetime
import sqlite3
import os
from pathlib import Path

from core.exceptions import DatabaseError

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
            self.connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False
            )
            self.connection.row_factory = sqlite3.Row

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
    # matches migration HEAD. modules/database/schema.sql remains as the
    # documented schema mirror only — nothing loads it at runtime.

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
            try:
                await self.execute("BEGIN TRANSACTION")
            except Exception:
                # Rollback state on failure — and release any waiters.
                self._in_transaction = False
                self._txn_owner = None
                self._txn_done.set()
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
                self._in_transaction = False
                self._txn_owner = None
                self._txn_done.set()

    async def rollback(self) -> None:
        """Rollback the current transaction."""
        async with self._transaction_lock:
            if not self._in_transaction:
                return  # Silently ignore if no transaction
            try:
                await self.execute("ROLLBACK")
            finally:
                # Always clear flag + release waiters, even on error
                self._in_transaction = False
                self._txn_owner = None
                self._txn_done.set()

    async def in_transaction(self) -> bool:
        """Check if currently in a transaction."""
        return self._in_transaction

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

                    must_wait = False
                    if (not is_transaction_control and self._in_transaction
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
                            must_wait = True
                            waiter = self._txn_done

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
            async with self._lock:
                if not self.connection:
                    await self.connect()
                    
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
            async with self._lock:
                if not self.connection:
                    await self.connect()
                    
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
            self.logger.error(f"Fetch all error: {e} | Query: {query} | Params: {params}")
            self.logger.exception("Detailed error:")
            return []

    async def executemany(self, query: str, param_list: List[Union[tuple, Dict[str, Any]]]) -> None:
        """Execute multiple queries with parameter lists."""
        try:
            async with self._lock:
                if not self.connection:
                    await self.connect()
                    
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
                self.connection.commit()
                self.logger.debug(f"Executed multiple queries: {query}")
        except Exception as e:
            self.logger.error(f"Executemany error: {e} | Query: {query}")
            raise

    async def get_table_names(self) -> List[str]:
        """Get all table names from the database."""
        query = "SELECT name FROM sqlite_master WHERE type='table';"
        rows = await self.fetch_all(query)
        return [row['name'] for row in rows]

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

def _auto_db_path() -> str:
    """Path to auto.db, anchored to the install/repo root (not the process CWD)."""
    from pathlib import Path
    return str(Path(__file__).resolve().parents[2] / "data" / "auto.db")


def get_db_connection():
    """Get database connection."""
    return sqlite3.connect(_auto_db_path())

def init_auto_tables():
    """Initialize auto-related tables."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create tables from schema
    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS auto_knowledge (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS auto_interactions (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS auto_cycles (
            id TEXT PRIMARY KEY,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            status TEXT,
            metrics JSON
        );
    ''')
    
    conn.commit()
    conn.close()