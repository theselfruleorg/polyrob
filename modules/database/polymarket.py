"""
Database handler for Polymarket credentials and audit logging.

Manages encrypted wallet credentials and trading activity audit trail.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from modules.database.connection import DatabaseConnection
from tools.mcp.security import MCPEncryption, get_encryption
from tools.polymarket.models import (
    PolymarketCredentials,
    TradingLimits,
    ApiCredentials,
    POLYGON_MAINNET,
    SIGNATURE_TYPE_PROXY,
)

logger = logging.getLogger(__name__)


class PolymarketDBHandler:
    """Handler for Polymarket database operations."""

    def __init__(
        self,
        db: DatabaseConnection,
        encryption: Optional[MCPEncryption] = None
    ):
        """
        Initialize handler.

        Args:
            db: Database connection
            encryption: Encryption instance (uses default if not provided)
        """
        self.db = db
        self.encryption = encryption or get_encryption()
        self.logger = logging.getLogger("database.polymarket")
        self._tables_initialized = False

    async def ensure_tables(self) -> None:
        """
        Ensure Polymarket tables exist and create them if not.

        Creates tables if they don't exist. Safe to call multiple times.
        """
        if self._tables_initialized:
            return

        self.logger.info("Ensuring Polymarket tables exist...")

        # Create polymarket_credentials table
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS polymarket_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL UNIQUE,
                wallet_address TEXT,
                private_key_encrypted BLOB,
                demo_mode INTEGER DEFAULT 1,
                enabled INTEGER DEFAULT 1,
                chain_id INTEGER DEFAULT 137,
                trading_limits TEXT DEFAULT '{}',
                api_key TEXT,
                api_secret_encrypted BLOB,
                api_passphrase_encrypted BLOB,
                api_credentials_created_at TEXT,
                connection_count INTEGER DEFAULT 0,
                last_connected_at TEXT,
                last_error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Add new columns if they don't exist (for migration)
        # Check existing columns first to avoid noisy error logs
        existing_columns = set()
        try:
            result = await self.db.fetch_all("PRAGMA table_info(polymarket_credentials)")
            existing_columns = {row['name'] for row in result}
        except Exception:
            pass  # Table might not exist yet, columns will be added with CREATE TABLE

        migration_columns = [
            ('chain_id', 'INTEGER DEFAULT 137'),
            ('api_key', 'TEXT'),
            ('api_secret_encrypted', 'BLOB'),
            ('api_passphrase_encrypted', 'BLOB'),
            ('api_credentials_created_at', 'TEXT'),
            # Proxy wallet support (added for Polymarket website users)
            ('proxy_wallet_address', 'TEXT'),
            ('signature_type', 'INTEGER DEFAULT 2'),  # Default to PROXY (2)
            ('allowances_verified', 'INTEGER DEFAULT 0'),
            ('last_balance_check', 'TEXT'),
        ]

        for col_name, col_type in migration_columns:
            if col_name not in existing_columns:
                try:
                    await self.db.execute(f'ALTER TABLE polymarket_credentials ADD COLUMN {col_name} {col_type}')
                    self.logger.debug(f"Added migration column: {col_name}")
                except Exception:
                    pass  # Column might have been added concurrently

        # Create polymarket_audit_log table
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS polymarket_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                tool_name TEXT,
                market_id TEXT,
                details TEXT,
                ip_address TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create indexes for performance
        await self.db.execute('''
            CREATE INDEX IF NOT EXISTS idx_polymarket_audit_user
            ON polymarket_audit_log(user_id)
        ''')
        await self.db.execute('''
            CREATE INDEX IF NOT EXISTS idx_polymarket_audit_action
            ON polymarket_audit_log(action)
        ''')
        await self.db.execute('''
            CREATE INDEX IF NOT EXISTS idx_polymarket_audit_timestamp
            ON polymarket_audit_log(timestamp)
        ''')

        self._tables_initialized = True
        self.logger.info("Polymarket tables ready")

    async def get_credentials(self, user_id: str) -> Optional[PolymarketCredentials]:
        """
        Get user's Polymarket credentials.

        Args:
            user_id: User ID

        Returns:
            PolymarketCredentials or None if not configured
        """
        query = """
            SELECT user_id, wallet_address, proxy_wallet_address, private_key_encrypted,
                   signature_type, demo_mode, enabled, chain_id, trading_limits,
                   api_key, api_secret_encrypted, api_passphrase_encrypted,
                   api_credentials_created_at, allowances_verified
            FROM polymarket_credentials
            WHERE user_id = ?
        """
        row = await self.db.fetch_one(query, (user_id,))

        if not row:
            return None

        # Decrypt private key
        private_key = None
        if row["private_key_encrypted"]:
            try:
                private_key = self.encryption.decrypt(row["private_key_encrypted"])
            except Exception as e:
                self.logger.error(f"Failed to decrypt private key for user {user_id}: {e}")

        # Parse trading limits
        limits_dict = {}
        if row["trading_limits"]:
            try:
                limits_dict = json.loads(row["trading_limits"])
            except json.JSONDecodeError:
                pass

        # Decrypt API credentials if present
        api_credentials = None
        if row.get("api_key"):
            try:
                api_secret = None
                api_passphrase = None
                if row.get("api_secret_encrypted"):
                    api_secret = self.encryption.decrypt(row["api_secret_encrypted"])
                if row.get("api_passphrase_encrypted"):
                    api_passphrase = self.encryption.decrypt(row["api_passphrase_encrypted"])

                api_credentials = ApiCredentials(
                    api_key=row["api_key"],
                    api_secret=api_secret or "",
                    api_passphrase=api_passphrase or "",
                    created_at=row.get("api_credentials_created_at")
                )
            except Exception as e:
                self.logger.error(f"Failed to decrypt API credentials for user {user_id}: {e}")

        return PolymarketCredentials(
            user_id=row["user_id"],
            wallet_address=row["wallet_address"],
            proxy_wallet_address=row.get("proxy_wallet_address"),
            private_key=private_key,
            signature_type=row.get("signature_type", SIGNATURE_TYPE_PROXY),
            demo_mode=bool(row["demo_mode"]),
            enabled=bool(row["enabled"]),
            chain_id=row.get("chain_id", POLYGON_MAINNET),
            trading_limits=TradingLimits.from_dict(limits_dict),
            api_credentials=api_credentials,
            allowances_verified=bool(row.get("allowances_verified", False))
        )

    async def save_credentials(
        self,
        user_id: str,
        wallet_address: Optional[str] = None,
        proxy_wallet_address: Optional[str] = None,
        private_key: Optional[str] = None,
        signature_type: int = SIGNATURE_TYPE_PROXY,
        demo_mode: bool = True,
        enabled: bool = True,
        chain_id: int = POLYGON_MAINNET,
        trading_limits: Optional[TradingLimits] = None,
        api_credentials: Optional[ApiCredentials] = None,
        allowances_verified: bool = False
    ) -> bool:
        """
        Save or update user's Polymarket credentials.

        Args:
            user_id: User ID
            wallet_address: EOA wallet address (derived from private key)
            proxy_wallet_address: Proxy wallet address (from Polymarket profile)
            private_key: Polygon private key (will be encrypted)
            signature_type: Signature type (0=EOA, 1=Magic, 2=Proxy)
            demo_mode: Whether to use demo mode
            enabled: Whether Polymarket is enabled
            chain_id: Polygon chain ID (137 mainnet, 80002 Amoy testnet)
            trading_limits: Trading safety limits
            api_credentials: L2 API credentials (will be encrypted)
            allowances_verified: Whether trading allowances are verified

        Returns:
            True if saved successfully
        """
        # Encrypt private key
        private_key_encrypted = None
        if private_key:
            private_key_encrypted = self.encryption.encrypt(private_key)

        # Serialize trading limits
        limits_json = json.dumps(
            (trading_limits or TradingLimits()).to_dict()
        )

        # Encrypt API credentials if provided
        api_key = None
        api_secret_encrypted = None
        api_passphrase_encrypted = None
        api_credentials_created_at = None
        if api_credentials:
            api_key = api_credentials.api_key
            if api_credentials.api_secret:
                api_secret_encrypted = self.encryption.encrypt(api_credentials.api_secret)
            if api_credentials.api_passphrase:
                api_passphrase_encrypted = self.encryption.encrypt(api_credentials.api_passphrase)
            api_credentials_created_at = api_credentials.created_at or datetime.utcnow().isoformat()

        query = """
            INSERT INTO polymarket_credentials
                (user_id, wallet_address, proxy_wallet_address, private_key_encrypted,
                 signature_type, demo_mode, enabled, chain_id, trading_limits,
                 api_key, api_secret_encrypted, api_passphrase_encrypted,
                 api_credentials_created_at, allowances_verified, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                wallet_address = COALESCE(excluded.wallet_address, wallet_address),
                proxy_wallet_address = COALESCE(excluded.proxy_wallet_address, proxy_wallet_address),
                private_key_encrypted = COALESCE(excluded.private_key_encrypted, private_key_encrypted),
                signature_type = excluded.signature_type,
                demo_mode = excluded.demo_mode,
                enabled = excluded.enabled,
                chain_id = excluded.chain_id,
                trading_limits = excluded.trading_limits,
                api_key = COALESCE(excluded.api_key, api_key),
                api_secret_encrypted = COALESCE(excluded.api_secret_encrypted, api_secret_encrypted),
                api_passphrase_encrypted = COALESCE(excluded.api_passphrase_encrypted, api_passphrase_encrypted),
                api_credentials_created_at = COALESCE(excluded.api_credentials_created_at, api_credentials_created_at),
                allowances_verified = excluded.allowances_verified,
                updated_at = excluded.updated_at
        """

        await self.db.execute(query, (
            user_id,
            wallet_address,
            proxy_wallet_address,
            private_key_encrypted,
            signature_type,
            1 if demo_mode else 0,
            1 if enabled else 0,
            chain_id,
            limits_json,
            api_key,
            api_secret_encrypted,
            api_passphrase_encrypted,
            api_credentials_created_at,
            1 if allowances_verified else 0,
            datetime.utcnow().isoformat()
        ))

        self.logger.info(f"Saved Polymarket credentials for user {user_id}")

        # Audit log
        await self.audit_log(user_id, "credentials_updated", details={
            "demo_mode": demo_mode,
            "enabled": enabled,
            "has_wallet": bool(wallet_address),
            "has_proxy_wallet": bool(proxy_wallet_address),
            "signature_type": signature_type,
            "has_key": bool(private_key),
            "has_api_creds": bool(api_credentials)
        })

        return True

    async def save_api_credentials(
        self,
        user_id: str,
        api_credentials: ApiCredentials
    ) -> bool:
        """
        Save L2 API credentials for a user (separate from wallet setup).

        Args:
            user_id: User ID
            api_credentials: L2 API credentials

        Returns:
            True if saved successfully
        """
        api_secret_encrypted = None
        api_passphrase_encrypted = None

        if api_credentials.api_secret:
            api_secret_encrypted = self.encryption.encrypt(api_credentials.api_secret)
        if api_credentials.api_passphrase:
            api_passphrase_encrypted = self.encryption.encrypt(api_credentials.api_passphrase)

        query = """
            UPDATE polymarket_credentials
            SET api_key = ?,
                api_secret_encrypted = ?,
                api_passphrase_encrypted = ?,
                api_credentials_created_at = ?,
                updated_at = ?
            WHERE user_id = ?
        """

        await self.db.execute(query, (
            api_credentials.api_key,
            api_secret_encrypted,
            api_passphrase_encrypted,
            api_credentials.created_at or datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat(),
            user_id
        ))

        self.logger.info(f"Saved API credentials for user {user_id}")

        await self.audit_log(user_id, "api_credentials_created", details={
            "api_key_prefix": api_credentials.api_key[:8] + "..." if api_credentials.api_key else None
        })

        return True

    async def update_connection_status(
        self,
        user_id: str,
        connected: bool,
        error: Optional[str] = None
    ) -> None:
        """
        Update connection status after connecting/disconnecting.

        Args:
            user_id: User ID
            connected: Whether connection was successful
            error: Error message if failed
        """
        if connected:
            query = """
                UPDATE polymarket_credentials
                SET last_connected_at = ?,
                    last_error = NULL,
                    connection_count = connection_count + 1,
                    updated_at = ?
                WHERE user_id = ?
            """
            now = datetime.utcnow().isoformat()
            await self.db.execute(query, (now, now, user_id))
        else:
            query = """
                UPDATE polymarket_credentials
                SET last_error = ?, updated_at = ?
                WHERE user_id = ?
            """
            await self.db.execute(query, (
                error,
                datetime.utcnow().isoformat(),
                user_id
            ))

    async def delete_credentials(self, user_id: str) -> bool:
        """
        Delete user's Polymarket credentials.

        Args:
            user_id: User ID

        Returns:
            True if deleted
        """
        query = "DELETE FROM polymarket_credentials WHERE user_id = ?"
        cursor = await self.db.execute(query, (user_id,))

        # Check if any rows were deleted using cursor.rowcount
        deleted = cursor.rowcount > 0 if hasattr(cursor, 'rowcount') else True

        if deleted:
            self.logger.info(f"Deleted Polymarket credentials for user {user_id}")
            await self.audit_log(user_id, "credentials_deleted")

        return deleted

    async def is_enabled(self, user_id: str) -> bool:
        """Check if Polymarket is enabled for user."""
        query = """
            SELECT enabled FROM polymarket_credentials
            WHERE user_id = ? AND enabled = 1
        """
        result = await self.db.fetch_one(query, (user_id,))
        return result is not None

    async def save_demo_credentials(self, user_id: str) -> None:
        """
        Save demo mode credentials for a user.

        This creates a minimal credentials record to track the user's
        demo mode session and prevent instance leaks from repeated
        in-memory credential creation.

        Args:
            user_id: User ID
        """
        query = """
            INSERT INTO polymarket_credentials
                (user_id, demo_mode, enabled, trading_limits, created_at, updated_at)
            VALUES (?, 1, 1, '{}', ?, ?)
            ON CONFLICT(user_id) DO NOTHING
        """
        now = datetime.utcnow().isoformat()
        await self.db.execute(query, (user_id, now, now))
        self.logger.debug(f"Created demo credentials for user {user_id}")

    async def audit_log(
        self,
        user_id: str,
        action: str,
        tool_name: Optional[str] = None,
        market_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None
    ) -> None:
        """
        Record an audit log entry.

        Args:
            user_id: User who performed the action
            action: Action type (e.g., 'tool_call', 'order_placed', 'credentials_updated')
            tool_name: Tool name if applicable
            market_id: Market ID if applicable
            details: Additional details (JSON serializable)
            ip_address: Client IP address
        """
        query = """
            INSERT INTO polymarket_audit_log
                (user_id, action, tool_name, market_id, details, ip_address)
            VALUES (?, ?, ?, ?, ?, ?)
        """

        details_json = json.dumps(details) if details else None

        await self.db.execute(query, (
            user_id,
            action,
            tool_name,
            market_id,
            details_json,
            ip_address
        ))

    async def get_audit_log(
        self,
        user_id: str,
        limit: int = 100,
        action: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get audit log entries for a user.

        Args:
            user_id: User ID
            limit: Maximum entries to return
            action: Filter by action type

        Returns:
            List of audit log entries
        """
        query = "SELECT * FROM polymarket_audit_log WHERE user_id = ?"
        params = [user_id]

        if action:
            query += " AND action = ?"
            params.append(action)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = await self.db.fetch_all(query, tuple(params))

        return [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "action": row["action"],
                "tool_name": row["tool_name"],
                "market_id": row["market_id"],
                "details": json.loads(row["details"]) if row["details"] else None,
                "ip_address": row["ip_address"],
                "timestamp": row["timestamp"]
            }
            for row in rows
        ]

    async def get_trading_stats(self, user_id: str) -> Dict[str, Any]:
        """
        Get trading statistics for a user.

        Args:
            user_id: User ID

        Returns:
            Dictionary with trading stats
        """
        # Count tool calls
        query = """
            SELECT
                COUNT(*) as total_calls,
                COUNT(CASE WHEN action = 'order_placed' THEN 1 END) as orders_placed,
                COUNT(CASE WHEN action = 'order_cancelled' THEN 1 END) as orders_cancelled,
                MIN(timestamp) as first_activity,
                MAX(timestamp) as last_activity
            FROM polymarket_audit_log
            WHERE user_id = ?
        """
        stats = await self.db.fetch_one(query, (user_id,))

        return {
            "total_calls": stats["total_calls"] or 0,
            "orders_placed": stats["orders_placed"] or 0,
            "orders_cancelled": stats["orders_cancelled"] or 0,
            "first_activity": stats["first_activity"],
            "last_activity": stats["last_activity"]
        }
