"""
Database handler for user MCP server configurations.

Manages CRUD operations for user_mcp_servers, user_mcp_settings, and user_mcp_audit_log tables.
"""

import json
import logging
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime

from modules.database.connection import DatabaseConnection
from tools.mcp.security import MCPEncryption, get_encryption

logger = logging.getLogger(__name__)


@dataclass
class UserMCPServer:
    """User MCP server record."""

    id: int
    user_id: str
    server_name: str
    server_url: str
    server_type: str
    auth_method: str
    enabled: bool
    timeout: int
    retry_attempts: int
    retry_delay: int
    auto_reconnect: bool
    max_concurrent_requests: int
    auth_status: str
    connection_count: int
    tools_discovered: int
    created_at: datetime
    updated_at: datetime
    display_name: Optional[str] = None
    last_connected_at: Optional[datetime] = None
    last_error: Optional[str] = None
    message_endpoint: Optional[str] = None  # For SSE servers with separate POST endpoint

    # Not stored directly - decrypted on demand
    api_key: Optional[str] = field(default=None, repr=False)
    headers: Optional[Dict[str, str]] = field(default=None, repr=False)


@dataclass
class UserMCPSettings:
    """User MCP settings record."""

    user_id: str
    mcp_enabled: bool = True
    include_global_servers: bool = True
    max_servers: int = 10
    default_timeout: int = 30
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UserMCPServersHandler:
    """Handler for user MCP server database operations."""

    # FIX #16: Schema version for migrations
    SCHEMA_VERSION = 2  # Increment when adding migrations

    # SECURITY FIX: Whitelist of allowed column names to prevent SQL injection
    # Only these columns can be updated via update_server() and update_connection_status()
    ALLOWED_UPDATE_COLUMNS = frozenset({
        'display_name', 'server_url', 'server_type', 'auth_method',
        'api_key_encrypted', 'headers_encrypted', 'enabled', 'timeout',
        'retry_attempts', 'retry_delay', 'auto_reconnect', 'max_concurrent_requests',
        'auth_status', 'last_connected_at', 'last_error', 'connection_count',
        'tools_discovered', 'updated_at', 'message_endpoint'
    })

    def __init__(self, db: DatabaseConnection, encryption: Optional[MCPEncryption] = None):
        """
        Initialize handler.

        Args:
            db: Database connection
            encryption: Encryption instance (uses default if not provided)
        """
        self.db = db
        self.encryption = encryption or get_encryption()
        self.logger = logging.getLogger('database.user_mcp_servers')
        self._tables_initialized = False

    async def ensure_tables(self) -> None:
        """
        Ensure all required tables exist and run migrations.

        Creates tables if they don't exist. Safe to call multiple times.
        Runs any pending schema migrations (FIX #16).
        """
        if self._tables_initialized:
            return

        self.logger.info("Ensuring user MCP tables exist...")
        
        # FIX #16: Create schema version table first
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS user_mcp_schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Get current schema version
        current_version = await self._get_schema_version()

        # Create user_mcp_servers table
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS user_mcp_servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                server_name TEXT NOT NULL,
                display_name TEXT,
                server_url TEXT NOT NULL,
                server_type TEXT NOT NULL,
                auth_method TEXT NOT NULL DEFAULT 'api_key',
                api_key_encrypted BLOB,
                headers_encrypted BLOB,
                enabled INTEGER DEFAULT 1,
                timeout INTEGER DEFAULT 30,
                retry_attempts INTEGER DEFAULT 3,
                retry_delay INTEGER DEFAULT 5,
                auto_reconnect INTEGER DEFAULT 1,
                max_concurrent_requests INTEGER DEFAULT 5,
                auth_status TEXT DEFAULT 'configured',
                last_connected_at TIMESTAMP,
                last_error TEXT,
                connection_count INTEGER DEFAULT 0,
                tools_discovered INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, server_name),
                CHECK (server_type IN ('sse', 'http', 'streamable_http')),
                CHECK (auth_method IN ('api_key', 'bearer', 'none'))
            )
        ''')

        # Create indices
        await self.db.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_mcp_servers_user
            ON user_mcp_servers(user_id)
        ''')
        await self.db.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_mcp_servers_enabled
            ON user_mcp_servers(user_id, enabled)
        ''')

        # Create user_mcp_settings table
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS user_mcp_settings (
                user_id TEXT PRIMARY KEY,
                mcp_enabled INTEGER DEFAULT 1,
                include_global_servers INTEGER DEFAULT 1,
                max_servers INTEGER DEFAULT 10,
                default_timeout INTEGER DEFAULT 30,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create user_mcp_audit_log table
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS user_mcp_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                server_name TEXT,
                details TEXT,
                ip_address TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create audit indices
        await self.db.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_mcp_audit_user
            ON user_mcp_audit_log(user_id)
        ''')
        await self.db.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_mcp_audit_time
            ON user_mcp_audit_log(timestamp)
        ''')

        # FIX #16: Run migrations if needed
        await self._run_migrations(current_version)

        self._tables_initialized = True
        self.logger.info("User MCP tables ready")

    async def _get_schema_version(self) -> int:
        """Get current schema version (FIX #16)."""
        try:
            result = await self.db.fetch_one(
                "SELECT version FROM user_mcp_schema_version WHERE id = 1"
            )
            return result[0] if result else 0
        except Exception:
            return 0

    async def _set_schema_version(self, version: int) -> None:
        """Set schema version (FIX #16)."""
        await self.db.execute('''
            INSERT INTO user_mcp_schema_version (id, version, updated_at)
            VALUES (1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET version = ?, updated_at = CURRENT_TIMESTAMP
        ''', (version, version))

    async def _run_migrations(self, current_version: int) -> None:
        """Run pending schema migrations (FIX #16)."""
        if current_version >= self.SCHEMA_VERSION:
            return
        
        self.logger.info(f"Running migrations from v{current_version} to v{self.SCHEMA_VERSION}")
        
        # Migration v1 -> v2: Add message_endpoint column
        if current_version < 2:
            # Check if column already exists before trying to add it
            try:
                result = await self.db.fetch_one(
                    "SELECT 1 FROM pragma_table_info('user_mcp_servers') WHERE name='message_endpoint'"
                )
                if not result:
                    await self.db.execute('''
                        ALTER TABLE user_mcp_servers 
                        ADD COLUMN message_endpoint TEXT
                    ''')
                    self.logger.info("Migration v2: Added message_endpoint column")
                else:
                    self.logger.debug("Migration v2: message_endpoint column already exists")
            except Exception as e:
                # Silently ignore duplicate column errors
                if "duplicate column" not in str(e).lower():
                    self.logger.warning(f"Migration v2 warning: {e}")
        
        # Update schema version
        await self._set_schema_version(self.SCHEMA_VERSION)
        self.logger.info(f"Schema migrated to v{self.SCHEMA_VERSION}")

    async def add_server(
        self,
        user_id: str,
        server_name: str,
        server_url: str,
        server_type: str = "sse",
        auth_method: str = "api_key",
        api_key: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        display_name: Optional[str] = None,
        **options
    ) -> UserMCPServer:
        """
        Add a new MCP server for a user.

        Args:
            user_id: Owner's user ID
            server_name: Unique name for the server
            server_url: Server endpoint URL
            server_type: 'sse' or 'http' (NOT 'stdio')
            auth_method: 'api_key', 'bearer', or 'none'
            api_key: API key (will be encrypted)
            headers: Custom headers (will be encrypted)
            display_name: Friendly display name
            **options: Additional options (enabled, timeout, etc.)

        Returns:
            Created UserMCPServer record

        Raises:
            ValueError: If server_type is 'stdio'
        """
        # Validate server_type
        if server_type not in ("sse", "http", "streamable_http"):
            raise ValueError("Server type must be 'sse', 'http', or 'streamable_http' (STDIO not allowed)")

        # Encrypt credentials
        api_key_encrypted = self.encryption.encrypt(api_key) if api_key else None
        headers_encrypted = self.encryption.encrypt_dict(headers) if headers else None

        query = """
            INSERT INTO user_mcp_servers (
                user_id, server_name, display_name, server_url, server_type,
                auth_method, api_key_encrypted, headers_encrypted,
                enabled, timeout, retry_attempts, retry_delay,
                auto_reconnect, max_concurrent_requests, auth_status, message_endpoint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        await self.db.execute(query, (
            user_id,
            server_name,
            display_name,
            server_url,
            server_type,
            auth_method,
            api_key_encrypted,
            headers_encrypted,
            1 if options.get('enabled', True) else 0,
            options.get('timeout', 30),
            options.get('retry_attempts', 3),
            options.get('retry_delay', 5),
            1 if options.get('auto_reconnect', True) else 0,
            options.get('max_concurrent_requests', 5),
            'configured' if api_key or auth_method == 'none' else 'unconfigured',
            options.get('message_endpoint')
        ))

        self.logger.info(f"Added MCP server '{server_name}' for user {user_id}")

        # Log audit event
        await self._audit_log(user_id, 'add', server_name, {
            'server_type': server_type,
            'auth_method': auth_method,
            'server_url': server_url
        })

        return await self.get_server(user_id, server_name)

    async def get_server(self, user_id: str, server_name: str) -> Optional[UserMCPServer]:
        """
        Get a specific server for a user.

        Args:
            user_id: Owner's user ID
            server_name: Server name

        Returns:
            UserMCPServer or None if not found
        """
        query = "SELECT * FROM user_mcp_servers WHERE user_id = ? AND server_name = ?"

        row = await self.db.fetch_one(query, (user_id, server_name))
        if row:
            return self._row_to_server(row)
        return None

    async def get_server_by_id(self, server_id: int) -> Optional[UserMCPServer]:
        """
        Get a server by its ID.

        Args:
            server_id: Server record ID

        Returns:
            UserMCPServer or None if not found
        """
        query = "SELECT * FROM user_mcp_servers WHERE id = ?"

        row = await self.db.fetch_one(query, (server_id,))
        if row:
            return self._row_to_server(row)
        return None

    async def get_user_servers(
        self,
        user_id: str,
        enabled_only: bool = False
    ) -> List[UserMCPServer]:
        """
        Get all MCP servers for a user.

        Args:
            user_id: Owner's user ID
            enabled_only: Only return enabled servers

        Returns:
            List of UserMCPServer records
        """
        query = "SELECT * FROM user_mcp_servers WHERE user_id = ?"
        params = [user_id]

        if enabled_only:
            query += " AND enabled = 1"

        query += " ORDER BY created_at DESC"

        rows = await self.db.fetch_all(query, tuple(params))
        return [self._row_to_server(row) for row in rows]

    async def update_server(
        self,
        user_id: str,
        server_name: str,
        **updates
    ) -> bool:
        """
        Update server configuration.

        Args:
            user_id: Owner's user ID
            server_name: Server name
            **updates: Fields to update

        Returns:
            True if updated successfully
        """
        if not updates:
            return False

        # Handle credential updates specially
        if 'api_key' in updates:
            api_key = updates.pop('api_key')
            updates['api_key_encrypted'] = self.encryption.encrypt(api_key) if api_key else None

        if 'headers' in updates:
            headers = updates.pop('headers')
            updates['headers_encrypted'] = self.encryption.encrypt_dict(headers) if headers else None

        # Convert boolean values to integers for SQLite
        for key in ['enabled', 'auto_reconnect']:
            if key in updates:
                updates[key] = 1 if updates[key] else 0

        updates['updated_at'] = datetime.utcnow().isoformat()

        # SECURITY FIX: Validate all column names against whitelist to prevent SQL injection
        invalid_columns = set(updates.keys()) - self.ALLOWED_UPDATE_COLUMNS
        if invalid_columns:
            self.logger.error(f"SQL injection attempt blocked: invalid columns {invalid_columns}")
            raise ValueError(f"Invalid column names: {invalid_columns}")

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        query = f"""
            UPDATE user_mcp_servers
            SET {set_clause}
            WHERE user_id = ? AND server_name = ?
        """

        result = await self.db.execute(
            query,
            tuple(list(updates.values()) + [user_id, server_name])
        )

        if result:
            self.logger.info(f"Updated MCP server '{server_name}' for user {user_id}")
            await self._audit_log(user_id, 'update', server_name, {
                'fields': list(updates.keys())
            })

        return result > 0 if isinstance(result, int) else True

    async def delete_server(self, user_id: str, server_name: str) -> bool:
        """
        Delete a server.

        Args:
            user_id: Owner's user ID
            server_name: Server name

        Returns:
            True if deleted successfully
        """
        query = "DELETE FROM user_mcp_servers WHERE user_id = ? AND server_name = ?"

        result = await self.db.execute(query, (user_id, server_name))

        if result:
            self.logger.info(f"Deleted MCP server '{server_name}' for user {user_id}")
            await self._audit_log(user_id, 'delete', server_name)

        return result > 0 if isinstance(result, int) else True

    async def get_decrypted_credentials(
        self,
        user_id: str,
        server_name: str
    ) -> tuple[Optional[str], Optional[Dict[str, str]]]:
        """
        Get decrypted API key and headers for a server.

        Args:
            user_id: Owner's user ID
            server_name: Server name

        Returns:
            Tuple of (api_key, headers) - both may be None
        """
        query = """
            SELECT api_key_encrypted, headers_encrypted
            FROM user_mcp_servers
            WHERE user_id = ? AND server_name = ?
        """

        row = await self.db.fetch_one(query, (user_id, server_name))

        if not row:
            return None, None

        api_key = self.encryption.decrypt(row['api_key_encrypted']) if row['api_key_encrypted'] else None
        headers = self.encryption.decrypt_dict(row['headers_encrypted']) if row['headers_encrypted'] else None

        return api_key, headers

    async def update_connection_status(
        self,
        user_id: str,
        server_name: str,
        connected: bool,
        error: Optional[str] = None,
        tools_count: Optional[int] = None
    ) -> bool:
        """
        Update server connection status.

        Args:
            user_id: Owner's user ID
            server_name: Server name
            connected: Whether connection was successful
            error: Error message if failed
            tools_count: Number of tools discovered

        Returns:
            True if updated
        """
        updates = {
            'last_error': error,
            'auth_status': 'connected' if connected else 'error',
            'updated_at': datetime.utcnow().isoformat()
        }

        if connected:
            updates['last_connected_at'] = datetime.utcnow().isoformat()
            updates['connection_count'] = 'connection_count + 1'  # Will need special handling

        if tools_count is not None:
            updates['tools_discovered'] = tools_count

        # Build query with special handling for increment
        set_parts = []
        values = []
        for k, v in updates.items():
            if v == 'connection_count + 1':
                set_parts.append("connection_count = connection_count + 1")
            else:
                set_parts.append(f"{k} = ?")
                values.append(v)

        query = f"""
            UPDATE user_mcp_servers
            SET {', '.join(set_parts)}
            WHERE user_id = ? AND server_name = ?
        """

        result = await self.db.execute(query, tuple(values + [user_id, server_name]))
        return result > 0 if isinstance(result, int) else True

    async def get_user_settings(self, user_id: str) -> UserMCPSettings:
        """
        Get user's MCP settings (creates defaults if not exist).

        Args:
            user_id: User ID

        Returns:
            UserMCPSettings record
        """
        query = "SELECT * FROM user_mcp_settings WHERE user_id = ?"
        row = await self.db.fetch_one(query, (user_id,))

        if row:
            return UserMCPSettings(
                user_id=row['user_id'],
                mcp_enabled=bool(row['mcp_enabled']),
                include_global_servers=bool(row['include_global_servers']),
                max_servers=row['max_servers'],
                default_timeout=row['default_timeout'],
                created_at=row.get('created_at'),
                updated_at=row.get('updated_at')
            )

        # Return defaults (not persisted until explicitly saved)
        return UserMCPSettings(user_id=user_id)

    async def update_user_settings(self, user_id: str, **updates) -> bool:
        """
        Update user's MCP settings (upsert).

        Args:
            user_id: User ID
            **updates: Settings to update

        Returns:
            True if updated
        """
        # Convert boolean values
        for key in ['mcp_enabled', 'include_global_servers']:
            if key in updates:
                updates[key] = 1 if updates[key] else 0

        # Upsert query
        columns = ['user_id'] + list(updates.keys()) + ['updated_at']
        values = [user_id] + list(updates.values()) + [datetime.utcnow().isoformat()]
        placeholders = ', '.join(['?'] * len(columns))

        update_clause = ', '.join(f"{k} = excluded.{k}" for k in updates.keys())
        update_clause += ", updated_at = excluded.updated_at"

        query = f"""
            INSERT INTO user_mcp_settings ({', '.join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(user_id) DO UPDATE SET {update_clause}
        """

        await self.db.execute(query, tuple(values))
        return True

    async def _audit_log(
        self,
        user_id: str,
        action: str,
        server_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None
    ) -> None:
        """
        Record an audit log entry.

        Args:
            user_id: User who performed the action
            action: Action type ('add', 'update', 'delete', 'connect', 'error')
            server_name: Server name if applicable
            details: Additional details (JSON serializable, no secrets!)
            ip_address: Client IP address
        """
        query = """
            INSERT INTO user_mcp_audit_log (user_id, action, server_name, details, ip_address)
            VALUES (?, ?, ?, ?, ?)
        """

        details_json = json.dumps(details) if details else None

        await self.db.execute(query, (
            user_id,
            action,
            server_name,
            details_json,
            ip_address
        ))

    async def get_audit_log(
        self,
        user_id: str,
        limit: int = 100,
        server_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get audit log entries for a user.

        Args:
            user_id: User ID
            limit: Maximum entries to return
            server_name: Filter by server name

        Returns:
            List of audit log entries
        """
        query = "SELECT * FROM user_mcp_audit_log WHERE user_id = ?"
        params = [user_id]

        if server_name:
            query += " AND server_name = ?"
            params.append(server_name)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = await self.db.fetch_all(query, tuple(params))

        return [
            {
                'id': row['id'],
                'user_id': row['user_id'],
                'action': row['action'],
                'server_name': row['server_name'],
                'details': json.loads(row['details']) if row['details'] else None,
                'ip_address': row['ip_address'],
                'timestamp': row['timestamp']
            }
            for row in rows
        ]

    def _row_to_server(self, row) -> UserMCPServer:
        """Convert database row to UserMCPServer object."""
        return UserMCPServer(
            id=row['id'],
            user_id=row['user_id'],
            server_name=row['server_name'],
            display_name=row.get('display_name'),
            server_url=row['server_url'],
            server_type=row['server_type'],
            auth_method=row['auth_method'],
            enabled=bool(row['enabled']),
            timeout=row['timeout'],
            retry_attempts=row['retry_attempts'],
            retry_delay=row['retry_delay'],
            auto_reconnect=bool(row['auto_reconnect']),
            max_concurrent_requests=row['max_concurrent_requests'],
            auth_status=row['auth_status'],
            last_connected_at=row.get('last_connected_at'),
            last_error=row.get('last_error'),
            connection_count=row['connection_count'],
            tools_discovered=row['tools_discovered'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            message_endpoint=row.get('message_endpoint')
        )

    async def server_count(self, user_id: str) -> int:
        """
        Get count of servers for a user.

        Args:
            user_id: User ID

        Returns:
            Number of servers
        """
        query = "SELECT COUNT(*) as count FROM user_mcp_servers WHERE user_id = ?"
        row = await self.db.fetch_one(query, (user_id,))
        return row['count'] if row else 0