"""Database manager module."""

import logging
import os
from typing import Dict, Any, Optional, List
from pathlib import Path
import json

from core.config import BotConfig
from core.exceptions import DatabaseError
from modules.base_module import BaseModule


def resolve_bot_db_path(config) -> Path:
    """THE bot.db resolution rule (R-2 B2, 2026-07-17).

    - ``DB_PATH`` env unset (the default): derive ``<data_dir>/database/bot.db``
      at open time — byte-identical to the historical behavior, and it follows a
      post-construction ``data_dir`` reassignment (the CLI container does this).
    - ``DB_PATH`` env set: honor ``config.db_path`` (config anchors it absolute),
      with a refuse-to-guess guard — if the configured path diverges from the
      derived one while the REAL database still sits at the derived location,
      raise instead of silently opening a fresh empty DB (phantom "data loss")
      or silently relocating a live one. The operator moves the file, we don't.
    """
    legacy = Path(config.data_dir) / "database" / "bot.db"
    configured_raw = os.getenv("DB_PATH")
    if not configured_raw:
        return legacy
    configured = Path(getattr(config, "db_path", configured_raw))
    if not configured.is_absolute():
        configured = Path(configured_raw)
    if (configured.resolve() != legacy.resolve()
            and legacy.is_file() and not configured.is_file()):
        raise RuntimeError(
            f"DB_PATH={configured} but the existing database is at {legacy}. "
            "Refusing to guess: stop the services, move bot.db (with its -wal/-shm "
            "siblings) to the configured path, then restart — or unset DB_PATH.")
    return configured

from .connection import DatabaseConnection
from .conversation_contexts import ConversationContexts
from .user_profiles import UserProfiles
from .auth_tables import AuthTables
from .x402_tables import X402Tables


class DatabaseManager(BaseModule):
    """Database manager with wallet-based authentication schema."""

    CORE_TABLES = {
        'user_profiles': UserProfiles,
        'conversation_contexts': ConversationContexts,
        'auth_tables': AuthTables,
        'x402_tables': X402Tables
    }
    
    def __init__(self, name: str, config: BotConfig, container=None):
        """Initialize database manager."""
        super().__init__(name=name, config=config, container=container)
        self.connection = None
        self.vector_storage = None
        self._tables_initialized = False
        self.tables = {}
        
    async def _initialize(self) -> None:
        """Initialize database and vector storage."""
        try:
            self.logger.info("Starting Database Manager initialization")
            
            # Initialize database connection
            self.logger.info("Initializing database connection")
            await self._init_connection()
            self.logger.info("Database connection established")
            
            # Initialize tables
            self.logger.info("Initializing database tables")
            await self._init_tables()
            self.logger.info("Database tables initialized")

            # Vector storage removed: cross-session semantic recall now lives in the
            # local sqlite-vec memory backend (LocalVectorMemoryProvider), not Pinecone.
            # `vector_storage` stays None for back-compat with has_vector_storage().

            self.logger.info("Database Manager initialization completed")

        except Exception as e:
            self.logger.error(f"Database initialization failed: {e}")
            raise

    async def _init_connection(self) -> None:
        """Initialize database connection."""
        try:
            db_path = resolve_bot_db_path(self.config)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            
            self.connection = DatabaseConnection(str(db_path))
            await self.connection.connect()
            
            self.logger.info(f"Connected to database at {db_path}")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize database connection: {e}")
            raise DatabaseError(f"Connection initialization failed: {e}")

    async def _init_tables(self) -> None:
        """Initialize database tables."""
        try:
            for table_name, table_class in self.CORE_TABLES.items():
                self.tables[table_name] = table_class(self.connection)
                # Some tables use create_tables() (plural) instead of create_table()
                if table_name in ['auth_tables', 'x402_tables']:
                    await self.tables[table_name].create_tables()
                else:
                    await self.tables[table_name].create_table()
                self.logger.info(f"Core table {table_name} created")

            self._tables_initialized = True

        except Exception as e:
            self.logger.error(f"Failed to initialize database tables: {e}")
            raise DatabaseError(f"Table initialization failed: {e}")

    async def _cleanup(self) -> None:
        """Clean up database resources."""
        try:
            if self.vector_storage:
                await self.vector_storage.cleanup()
                self.logger.info("Vector storage cleaned up")

            if self.connection:
                await self.connection.close()
                self.logger.info("Database connection closed")

        except Exception as e:
            self.logger.error(f"Database cleanup failed: {e}")
            raise DatabaseError(f"Failed to clean up database: {e}")

    def get_table(self, table_name: str) -> Any:
        """Get a table instance by name."""
        if not self._tables_initialized:
            raise DatabaseError("Database tables not initialized")
        
        table = self.tables.get(table_name)
        if not table:
            self.logger.error(f"Table {table_name} not found")
            raise DatabaseError(f"Table {table_name} not found")
        
        return table

    @property
    def conversation_contexts(self):
        """Get conversation contexts table."""
        return self.get_table('conversation_contexts')

    @property
    def user_profiles(self):
        """Get user profiles table."""
        return self.get_table('user_profiles')

    @property
    def has_vector_storage(self) -> bool:
        """Check if vector storage is available."""
        return self.vector_storage is not None and self.vector_storage._initialized

    @property
    def required_services(self) -> Dict[str, str]:
        """Get required services."""
        return {}

    async def execute(self, query: str, *args) -> Any:
        """Execute a raw SQL query."""
        try:
            return await self.connection.execute(query, *args)
        except Exception as e:
            self.logger.error(f"Failed to execute query: {e}")
            raise DatabaseError(f"Failed to execute query: {e}")

    async def fetch_one(self, query: str, *args) -> Optional[Dict[str, Any]]:
        """Fetch a single row from the database."""
        if not self.connection:
            raise DatabaseError("Database not initialized")
        return await self.connection.fetch_one(query, *args)

    async def fetch_all(self, query: str, *args) -> List[Dict[str, Any]]:
        """Fetch all rows from the database."""
        if not self.connection:
            raise DatabaseError("Database not initialized")
        return await self.connection.fetch_all(query, *args)

    # Compatibility aliases for legacy code
    async def fetch(self, query: str, *args) -> List[Dict[str, Any]]:
        """Alias for fetch_all to maintain backward compatibility."""
        return await self.fetch_all(query, *args)

    async def fetchone(self, query: str, *args) -> Optional[Dict[str, Any]]:
        """Alias for fetch_one to maintain backward compatibility."""
        return await self.fetch_one(query, *args)

    # SECURITY: Whitelist of allowed table names for upsert operations
    # This prevents SQL injection through table_name parameter
    ALLOWED_UPSERT_TABLES = frozenset({
        'user_profiles',
        'conversation_contexts',
        'user_credits',
        'credit_transactions',
        'auth_nonces',
        'auth_sessions',
        'jwt_tokens',
        'api_keys',
        'deposits',
        'billing_failures',
        'user_mcp_servers',
        'user_mcp_audit_log',
    })

    async def upsert(self, table_name: str, data: Dict[str, Any]) -> None:
        """Insert or update record in the specified table.

        SECURITY: Table names are validated against a whitelist to prevent SQL injection.
        Column names are validated to contain only safe characters.

        Args:
            table_name: Name of the table to upsert into (must be in ALLOWED_UPSERT_TABLES)
            data: Dictionary of column name -> value pairs

        Raises:
            DatabaseError: If table_name is not in whitelist or column names are invalid
        """
        import re

        try:
            # SECURITY FIX: Validate table name against whitelist
            if table_name not in self.ALLOWED_UPSERT_TABLES:
                self.logger.error(f"SECURITY: Attempted upsert to non-whitelisted table: {table_name}")
                raise DatabaseError(f"Table '{table_name}' is not allowed for upsert operations")

            filtered_data = {k: v for k, v in data.items() if v is not None}

            # SECURITY FIX: Validate column names contain only safe characters
            # Allowed: alphanumeric, underscore (standard SQL identifier characters)
            column_pattern = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
            for col_name in filtered_data.keys():
                if not column_pattern.match(col_name):
                    self.logger.error(f"SECURITY: Invalid column name in upsert: {col_name}")
                    raise DatabaseError(f"Invalid column name: '{col_name}'")

            columns = ', '.join(filtered_data.keys())
            placeholders = ', '.join(['?' for _ in filtered_data])
            values = list(filtered_data.values())

            # Now safe to use f-string since table_name and columns are validated
            query = f"""
                INSERT OR REPLACE INTO {table_name} ({columns})
                VALUES ({placeholders})
            """

            await self.connection.execute(query, values)
            self.logger.debug(f"Upserted record into {table_name}")

        except DatabaseError:
            raise  # Re-raise our security errors
        except Exception as e:
            self.logger.error(f"Failed to upsert record into {table_name}: {e}")
            self.logger.error(f"Failed data: {data}")
            raise DatabaseError(f"Failed to upsert record into {table_name}: {e}")
