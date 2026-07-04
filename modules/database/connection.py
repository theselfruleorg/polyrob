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

    async def _initialize_base_schema(self) -> None:
        """Initialize base tables schema."""
        try:
            # Create base tables first
            base_tables = """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                is_bot BOOLEAN,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                language_code TEXT,
                role TEXT DEFAULT 'user',
                preferences TEXT,
                wallet_address TEXT,
                den_password TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS conversation_contexts (
                conversation_id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                user_id TEXT,
                chat_id TEXT NOT NULL,
                chat_name TEXT,
                messages TEXT NOT NULL,
                metadata TEXT,
                last_interaction TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
            );
            """
            
            # Execute base tables creation
            statements = [stmt.strip() for stmt in base_tables.split(';') if stmt.strip()]
            for statement in statements:
                if statement:
                    self.connection.execute(statement)
            self.connection.commit()

            # Add role column if it doesn't exist
            try:
                self.connection.execute("""
                    ALTER TABLE user_profiles ADD COLUMN role TEXT DEFAULT 'user'
                """)
                self.connection.commit()
            except Exception as e:
                # Column might already exist, which is fine
                if 'duplicate column name' not in str(e).lower():
                    raise
            
        except Exception as e:
            self.logger.error(f"Error initializing base schema: {e}")
            raise

    async def _ensure_required_columns(self) -> None:
        """Ensure all required columns exist in tables."""
        try:
            # Get current columns in conversation_contexts
            cursor = self.connection.execute("PRAGMA table_info(conversation_contexts)")
            columns = {col[1] for col in cursor.fetchall()}
            
            # Add missing columns
            required_columns = [
                ('mode', 'TEXT DEFAULT "active"'),
                ('mode_metadata', 'TEXT'),
                ('keywords', 'TEXT')
            ]
            
            for column, definition in required_columns:
                if column not in columns:
                    try:
                        self.connection.execute(
                            f"ALTER TABLE conversation_contexts ADD COLUMN {column} {definition}"
                        )
                        self.logger.debug(f"Added column {column} to conversation_contexts")
                    except Exception as e:
                        if "duplicate column name" not in str(e).lower():
                            raise
            
            self.connection.commit()
            
        except Exception as e:
            self.logger.error(f"Error ensuring required columns: {e}")
            raise

    async def _initialize_remaining_schema(self) -> None:
        """Initialize remaining schema after base tables and columns are set up."""
        try:
            schema_path = Path(__file__).parent / 'schema.sql'
            with open(schema_path, 'r') as f:
                schema = f.read()
            
            # Execute remaining schema statements
            statements = [stmt.strip() for stmt in schema.split(';') if stmt.strip()]
            for statement in statements:
                if statement and (
                    statement.upper().startswith('CREATE TABLE') or 
                    statement.upper().startswith('CREATE INDEX')
                ):
                    try:
                        self.connection.execute(statement)
                    except Exception as e:
                        if "already exists" not in str(e):
                            raise
            
            self.connection.commit()
            self.logger.info("Remaining schema initialized successfully")
            
        except Exception as e:
            self.logger.error(f"Error initializing remaining schema: {e}")
            raise

    def _split_sql_statements(self, sql: str) -> List[str]:
        """Carefully split SQL statements preserving triggers and functions."""
        if not sql or not isinstance(sql, str):
            self.logger.error("❌ Invalid SQL input for splitting")
            raise ValueError("Invalid SQL input")
        
        statements = []
        current_statement = []
        in_string = False
        string_char = None
        
        try:
            # Add check for None before processing
            if sql is None:
                return []
            
            for line in sql.splitlines():
                line = line.strip()
                
                # Skip empty lines
                if not line:
                    continue
                
                # Handle comments
                if line.startswith('--'):
                    continue
                
                # Remove inline comments while preserving strings
                cleaned_line = ''
                i = 0
                while i < len(line):
                    if line[i:i+2] == '--' and not in_string:
                        break
                    elif line[i] in ["'", '"'] and (i == 0 or line[i-1] != '\\'):
                        if not in_string:
                            in_string = True
                            string_char = line[i]
                        elif line[i] == string_char:
                            in_string = False
                    cleaned_line += line[i]
                    i += 1
                
                if cleaned_line:
                    current_statement.append(cleaned_line)
                
                # Check if statement is complete
                if cleaned_line.rstrip().endswith(';') and not in_string:
                    full_statement = ' '.join(current_statement).strip()
                    if full_statement:
                        statements.append(full_statement)
                    current_statement = []
            
            # Handle any remaining statement
            if current_statement:
                full_statement = ' '.join(current_statement).strip()
                if full_statement:
                    if not full_statement.endswith(';'):
                        full_statement += ';'
                    statements.append(full_statement)
            
            self.logger.debug(f"Successfully split SQL into {len(statements)} statements")
            return statements
        
        except Exception as e:
            self.logger.error(f"SQL splitting error: {str(e)}")
            return []

    async def begin_transaction(self) -> None:
        """Begin a new transaction.

        SECURITY FIX: Set _in_transaction flag BEFORE calling execute() to prevent
        race condition where another coroutine could auto-commit during the window
        between execute() returning and flag being set.
        """
        async with self._transaction_lock:
            if self._in_transaction:
                raise DatabaseError("Transaction already in progress")
            # Set flag BEFORE execute to close race window
            self._in_transaction = True
            try:
                await self.execute("BEGIN TRANSACTION")
            except Exception:
                # Rollback state on failure
                self._in_transaction = False
                raise

    async def commit(self) -> None:
        """Commit the current transaction."""
        async with self._transaction_lock:
            if not self._in_transaction:
                raise DatabaseError("No transaction in progress")
            try:
                await self.execute("COMMIT")
            finally:
                # Always clear flag, even on error (transaction is no longer active)
                self._in_transaction = False

    async def rollback(self) -> None:
        """Rollback the current transaction."""
        async with self._transaction_lock:
            if not self._in_transaction:
                return  # Silently ignore if no transaction
            try:
                await self.execute("ROLLBACK")
            finally:
                # Always clear flag, even on error
                self._in_transaction = False

    async def in_transaction(self) -> bool:
        """Check if currently in a transaction."""
        return self._in_transaction

    async def execute(self, query: str, params: Union[tuple, Dict[str, Any]] = ()) -> aiosqlite.Cursor:
        """Execute SQL query."""
        try:
            async with self._lock:
                if not self.connection:
                    await self.connect()

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

                # Only auto-commit if NOT in an explicit transaction AND not a transaction control statement
                query_upper = query.strip().upper()
                is_transaction_control = query_upper.startswith(('BEGIN', 'COMMIT', 'ROLLBACK'))
                if not self._in_transaction and not is_transaction_control:
                    self.connection.commit()

                return cursor
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