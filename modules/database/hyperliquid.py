"""
Hyperliquid Database Handler

Manages credential storage, audit logging, and trading statistics.
"""

import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

from modules.database.connection import DatabaseConnection
from tools.mcp.security import MCPEncryption, get_encryption
from tools.hyperliquid.models import (
    HyperliquidCredentials,
    TradingLimits,
    AgentWallet,
)


class HyperliquidDBHandler:
    """Database handler for Hyperliquid credentials and audit logs"""

    def __init__(
        self,
        db: DatabaseConnection,
        encryption: Optional[MCPEncryption] = None
    ):
        """
        Initialize the database handler.

        Args:
            db: Database connection
            encryption: Encryption instance (uses default if not provided)
        """
        self.db = db
        self.encryption = encryption or get_encryption()
        self.logger = logging.getLogger("database.hyperliquid")
        self._tables_initialized = False

    async def ensure_tables(self) -> None:
        """Create tables if they don't exist"""
        if self._tables_initialized:
            return

        self.logger.info("Ensuring Hyperliquid tables exist...")

        # Main credentials table
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS hyperliquid_credentials (
                user_id TEXT PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                private_key_encrypted BLOB,
                agent_wallet_address TEXT,
                agent_wallet_private_key_encrypted BLOB,
                agent_wallet_name TEXT,
                testnet INTEGER DEFAULT 1,
                demo_mode INTEGER DEFAULT 1,
                enabled INTEGER DEFAULT 1,
                trading_limits TEXT DEFAULT '{}',
                connection_count INTEGER DEFAULT 0,
                last_connected_at TEXT,
                last_error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Audit log table
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS hyperliquid_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                tool_name TEXT,
                market_id TEXT,
                details TEXT DEFAULT '{}',
                ip_address TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create indexes
        await self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_hl_audit_user
            ON hyperliquid_audit_log(user_id)
        """)
        await self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_hl_audit_action
            ON hyperliquid_audit_log(action)
        """)
        await self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_hl_audit_timestamp
            ON hyperliquid_audit_log(timestamp)
        """)

        self._tables_initialized = True
        self.logger.info("Hyperliquid tables ready")

    async def get_credentials(self, user_id: str) -> Optional[HyperliquidCredentials]:
        """Get credentials for a user (decrypts secrets)"""
        row = await self.db.fetch_one(
            "SELECT * FROM hyperliquid_credentials WHERE user_id = ?",
            (user_id,)
        )

        if not row:
            return None

        # Decrypt secrets
        private_key = ""
        if row["private_key_encrypted"]:
            try:
                private_key = self.encryption.decrypt(row["private_key_encrypted"])
            except Exception as e:
                self.logger.error(f"Failed to decrypt private key: {e}")

        # Build agent wallet if present
        agent_wallet = None
        if row["agent_wallet_address"]:
            agent_private_key = ""
            if row["agent_wallet_private_key_encrypted"]:
                try:
                    agent_private_key = self.encryption.decrypt(
                        row["agent_wallet_private_key_encrypted"]
                    )
                except Exception:
                    pass

            agent_wallet = AgentWallet(
                address=row["agent_wallet_address"],
                private_key=agent_private_key,
                name=row["agent_wallet_name"],
            )

        # Parse trading limits
        try:
            limits_data = json.loads(row["trading_limits"] or "{}")
            trading_limits = TradingLimits.from_dict(limits_data)
        except (json.JSONDecodeError, TypeError):
            trading_limits = TradingLimits()

        # Parse last connected timestamp
        last_connected = None
        if row["last_connected_at"]:
            try:
                last_connected = datetime.fromisoformat(row["last_connected_at"])
            except ValueError:
                pass

        return HyperliquidCredentials(
            user_id=row["user_id"],
            wallet_address=row["wallet_address"],
            private_key=private_key,
            agent_wallet=agent_wallet,
            testnet=bool(row["testnet"]),
            demo_mode=bool(row["demo_mode"]),
            enabled=bool(row["enabled"]),
            trading_limits=trading_limits,
            connection_count=row["connection_count"] or 0,
            last_connected_at=last_connected,
            last_error=row["last_error"],
        )

    async def save_credentials(
        self,
        user_id: str,
        wallet_address: str,
        private_key: str,
        agent_wallet: Optional[AgentWallet] = None,
        testnet: bool = True,
        demo_mode: bool = True,
        trading_limits: Optional[TradingLimits] = None,
    ) -> None:
        """Save or update user credentials (encrypts secrets)"""
        encrypted_key = self.encryption.encrypt(private_key)

        agent_address = None
        agent_key_encrypted = None
        agent_name = None
        if agent_wallet:
            agent_address = agent_wallet.address
            agent_key_encrypted = self.encryption.encrypt(agent_wallet.private_key)
            agent_name = agent_wallet.name

        limits_json = json.dumps((trading_limits or TradingLimits()).to_dict())

        await self.db.execute("""
            INSERT INTO hyperliquid_credentials
                (user_id, wallet_address, private_key_encrypted,
                 agent_wallet_address, agent_wallet_private_key_encrypted, agent_wallet_name,
                 testnet, demo_mode, trading_limits, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                wallet_address = excluded.wallet_address,
                private_key_encrypted = excluded.private_key_encrypted,
                agent_wallet_address = excluded.agent_wallet_address,
                agent_wallet_private_key_encrypted = excluded.agent_wallet_private_key_encrypted,
                agent_wallet_name = excluded.agent_wallet_name,
                testnet = excluded.testnet,
                demo_mode = excluded.demo_mode,
                trading_limits = excluded.trading_limits,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, wallet_address, encrypted_key,
              agent_address, agent_key_encrypted, agent_name,
              int(testnet), int(demo_mode), limits_json))

        self.logger.info(f"Saved credentials for user {user_id[:8]}...")

    async def update_trading_limits(
        self,
        user_id: str,
        trading_limits: TradingLimits
    ) -> bool:
        """Update trading limits for a user"""
        limits_json = json.dumps(trading_limits.to_dict())
        result = await self.db.execute("""
            UPDATE hyperliquid_credentials
            SET trading_limits = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (limits_json, user_id))
        return result > 0

    async def update_connection_status(
        self,
        user_id: str,
        success: bool,
        error: Optional[str] = None
    ) -> None:
        """Update connection status after API call"""
        if success:
            await self.db.execute("""
                UPDATE hyperliquid_credentials
                SET connection_count = connection_count + 1,
                    last_connected_at = CURRENT_TIMESTAMP,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """, (user_id,))
        else:
            await self.db.execute("""
                UPDATE hyperliquid_credentials
                SET last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """, (error, user_id))

    async def set_enabled(self, user_id: str, enabled: bool) -> bool:
        """Enable or disable Hyperliquid for a user"""
        result = await self.db.execute("""
            UPDATE hyperliquid_credentials
            SET enabled = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (int(enabled), user_id))
        return result > 0

    async def set_demo_mode(self, user_id: str, demo_mode: bool) -> bool:
        """Set demo mode for a user"""
        result = await self.db.execute("""
            UPDATE hyperliquid_credentials
            SET demo_mode = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (int(demo_mode), user_id))
        return result > 0

    async def delete_credentials(self, user_id: str) -> bool:
        """Permanently delete credentials"""
        result = await self.db.execute(
            "DELETE FROM hyperliquid_credentials WHERE user_id = ?",
            (user_id,)
        )
        deleted = result > 0
        if deleted:
            self.logger.info(f"Deleted credentials for user {user_id[:8]}...")
        return deleted

    async def audit_log(
        self,
        user_id: str,
        action: str,
        tool_name: Optional[str] = None,
        market_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
    ) -> None:
        """Log an action for audit purposes"""
        await self.db.execute("""
            INSERT INTO hyperliquid_audit_log
                (user_id, action, tool_name, market_id, details, ip_address)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, action, tool_name, market_id,
              json.dumps(details or {}), ip_address))

    async def get_audit_log(
        self,
        user_id: str,
        limit: int = 50,
        action: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get audit log entries for a user"""
        if action:
            rows = await self.db.fetch_all(
                """SELECT * FROM hyperliquid_audit_log
                   WHERE user_id = ? AND action = ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (user_id, action, limit)
            )
        else:
            rows = await self.db.fetch_all(
                """SELECT * FROM hyperliquid_audit_log
                   WHERE user_id = ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (user_id, limit)
            )

        return [
            {
                "id": row["id"],
                "action": row["action"],
                "tool_name": row["tool_name"],
                "market_id": row["market_id"],
                "details": json.loads(row["details"] or "{}"),
                "ip_address": row["ip_address"],
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]

    async def get_trading_stats(self, user_id: str) -> Dict[str, Any]:
        """Get aggregated trading statistics"""
        row = await self.db.fetch_one("""
            SELECT
                SUM(CASE WHEN action = 'place_limit_order' THEN 1 ELSE 0 END) as orders_placed,
                SUM(CASE WHEN action = 'cancel_order' THEN 1 ELSE 0 END) as orders_cancelled,
                SUM(CASE WHEN action = 'cancel_all_orders' THEN 1 ELSE 0 END) as bulk_cancels,
                SUM(CASE WHEN action = 'update_leverage' THEN 1 ELSE 0 END) as leverage_updates,
                MIN(timestamp) as first_action,
                MAX(timestamp) as last_action,
                COUNT(*) as total_actions
            FROM hyperliquid_audit_log
            WHERE user_id = ?
        """, (user_id,))

        if not row:
            return {
                "orders_placed": 0,
                "orders_cancelled": 0,
                "bulk_cancels": 0,
                "leverage_updates": 0,
                "first_action": None,
                "last_action": None,
                "total_actions": 0,
            }

        return {
            "orders_placed": row["orders_placed"] or 0,
            "orders_cancelled": row["orders_cancelled"] or 0,
            "bulk_cancels": row["bulk_cancels"] or 0,
            "leverage_updates": row["leverage_updates"] or 0,
            "first_action": row["first_action"],
            "last_action": row["last_action"],
            "total_actions": row["total_actions"] or 0,
        }

    async def get_recent_orders(
        self,
        user_id: str,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Get recent order actions from audit log"""
        return await self.get_audit_log(
            user_id=user_id,
            limit=limit,
            action="place_limit_order"
        )
