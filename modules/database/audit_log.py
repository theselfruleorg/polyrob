"""Audit logging for security-sensitive operations.

This module provides a dedicated audit trail for:
- Role/tier changes
- Credit modifications
- Admin actions
- Authentication events
- Permission changes
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any
import json

from modules.database.connection import DatabaseConnection

logger = logging.getLogger('database.audit_log')


class AuditLogger:
    """Audit logger for security-sensitive operations."""

    # Audit event types
    EVENT_AUTH_SUCCESS = "auth_success"
    EVENT_AUTH_FAILURE = "auth_failure"
    EVENT_ROLE_CHANGE = "role_change"
    EVENT_TIER_CHANGE = "tier_change"
    EVENT_CREDIT_ADD = "credit_add"
    EVENT_CREDIT_DEDUCT = "credit_deduct"
    EVENT_ADMIN_ACTION = "admin_action"
    EVENT_API_KEY_CREATE = "api_key_create"
    EVENT_API_KEY_REVOKE = "api_key_revoke"
    EVENT_PERMISSION_CHANGE = "permission_change"
    EVENT_SECURITY_ALERT = "security_alert"
    EVENT_ADMIN_WALLET_AUTH = "admin_wallet_auth"

    def __init__(self, db: DatabaseConnection):
        self.db = db
        self.logger = logging.getLogger('audit')

    async def create_table(self) -> None:
        """Create the audit log table."""
        try:
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    event_type TEXT NOT NULL,
                    actor_id TEXT,
                    actor_wallet TEXT,
                    actor_ip TEXT,
                    target_id TEXT,
                    target_type TEXT,
                    action TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    metadata TEXT,
                    request_id TEXT,
                    success INTEGER DEFAULT 1
                )
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_id)
            ''')

            self.logger.info("✅ Audit log table created successfully")

        except Exception as e:
            self.logger.error(f"Error creating audit log table: {e}")
            raise

    async def log(
        self,
        event_type: str,
        action: str,
        actor_id: Optional[str] = None,
        actor_wallet: Optional[str] = None,
        actor_ip: Optional[str] = None,
        target_id: Optional[str] = None,
        target_type: Optional[str] = None,
        old_value: Optional[Any] = None,
        new_value: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        success: bool = True
    ) -> int:
        """Log an audit event.

        Args:
            event_type: Type of event (use EVENT_* constants)
            action: Human-readable description of the action
            actor_id: User ID of who performed the action
            actor_wallet: Wallet address of actor (if applicable)
            actor_ip: IP address of the actor
            target_id: ID of the target entity (user, resource, etc.)
            target_type: Type of target entity
            old_value: Previous value (for changes)
            new_value: New value (for changes)
            metadata: Additional context as dict
            request_id: Request/correlation ID
            success: Whether the action succeeded

        Returns:
            The audit log entry ID
        """
        try:
            # Serialize values to JSON if not strings
            old_val_str = json.dumps(old_value) if old_value is not None else None
            new_val_str = json.dumps(new_value) if new_value is not None else None
            metadata_str = json.dumps(metadata) if metadata else None

            result = await self.db.execute('''
                INSERT INTO audit_log (
                    event_type, action, actor_id, actor_wallet, actor_ip,
                    target_id, target_type, old_value, new_value,
                    metadata, request_id, success
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                event_type, action, actor_id, actor_wallet, actor_ip,
                target_id, target_type, old_val_str, new_val_str,
                metadata_str, request_id, 1 if success else 0
            ))

            # Also log to standard logger for immediate visibility
            log_msg = f"AUDIT: {event_type} | {action}"
            if actor_id:
                log_msg += f" | actor={actor_id}"
            if target_id:
                log_msg += f" | target={target_id}"
            if not success:
                log_msg += " | FAILED"

            if success:
                self.logger.info(log_msg)
            else:
                self.logger.warning(log_msg)

            return result.lastrowid if hasattr(result, 'lastrowid') else 0

        except Exception as e:
            self.logger.error(f"Failed to write audit log: {e}")
            # Don't raise - audit logging should not break the main flow
            return 0

    async def log_auth(
        self,
        wallet_address: str,
        success: bool,
        ip_address: Optional[str] = None,
        failure_reason: Optional[str] = None
    ) -> int:
        """Log authentication attempt."""
        event_type = self.EVENT_AUTH_SUCCESS if success else self.EVENT_AUTH_FAILURE
        action = "User authenticated" if success else f"Authentication failed: {failure_reason or 'unknown'}"

        return await self.log(
            event_type=event_type,
            action=action,
            actor_wallet=wallet_address,
            actor_ip=ip_address,
            success=success,
            metadata={"failure_reason": failure_reason} if failure_reason else None
        )

    async def log_admin_wallet_auth(
        self,
        wallet_address: str,
        user_id: str,
        ip_address: Optional[str] = None,
    ) -> int:
        """Log a session-based admin-privilege grant from an admin-listed wallet
        (ADMIN_WALLETS) logging in. Distinct from a persistent role change —
        api/auth_endpoints.py deliberately does NOT escalate the DB role here; this
        is the audit trail for the session privileges that ARE granted (E3)."""
        return await self.log(
            event_type=self.EVENT_ADMIN_WALLET_AUTH,
            action="Admin wallet authenticated (session privileges granted)",
            actor_id=user_id,
            actor_wallet=wallet_address,
            actor_ip=ip_address,
            target_id=user_id,
            target_type="user",
        )

    async def log_role_change(
        self,
        admin_id: str,
        target_user_id: str,
        old_role: str,
        new_role: str,
        ip_address: Optional[str] = None
    ) -> int:
        """Log role change."""
        return await self.log(
            event_type=self.EVENT_ROLE_CHANGE,
            action=f"Role changed from '{old_role}' to '{new_role}'",
            actor_id=admin_id,
            actor_ip=ip_address,
            target_id=target_user_id,
            target_type="user",
            old_value=old_role,
            new_value=new_role
        )

    async def log_tier_change(
        self,
        admin_id: str,
        target_user_id: str,
        old_tier: str,
        new_tier: str,
        ip_address: Optional[str] = None
    ) -> int:
        """Log tier change."""
        return await self.log(
            event_type=self.EVENT_TIER_CHANGE,
            action=f"Tier changed from '{old_tier}' to '{new_tier}'",
            actor_id=admin_id,
            actor_ip=ip_address,
            target_id=target_user_id,
            target_type="user",
            old_value=old_tier,
            new_value=new_tier
        )

    async def log_credit_change(
        self,
        admin_id: str,
        target_user_id: str,
        amount: int,
        is_addition: bool,
        reason: str,
        balance_before: int,
        balance_after: int,
        ip_address: Optional[str] = None
    ) -> int:
        """Log credit modification."""
        event_type = self.EVENT_CREDIT_ADD if is_addition else self.EVENT_CREDIT_DEDUCT
        action = f"{'Added' if is_addition else 'Deducted'} {abs(amount)} credits: {reason}"

        return await self.log(
            event_type=event_type,
            action=action,
            actor_id=admin_id,
            actor_ip=ip_address,
            target_id=target_user_id,
            target_type="user",
            old_value=balance_before,
            new_value=balance_after,
            metadata={"amount": amount, "reason": reason}
        )

    async def log_security_alert(
        self,
        alert_type: str,
        description: str,
        actor_id: Optional[str] = None,
        actor_ip: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """Log security alert."""
        return await self.log(
            event_type=self.EVENT_SECURITY_ALERT,
            action=f"SECURITY ALERT: {alert_type} - {description}",
            actor_id=actor_id,
            actor_ip=actor_ip,
            metadata=metadata,
            success=False  # Alerts are always logged as failures for visibility
        )

    async def get_recent_events(
        self,
        limit: int = 100,
        offset: int = 0,
        event_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        target_id: Optional[str] = None
    ) -> list:
        """Get recent audit events.

        Args:
            limit: Maximum number of events to return
            offset: Number of events to skip (for pagination)
            event_type: Filter by event type
            actor_id: Filter by actor user ID
            target_id: Filter by target user ID

        Returns:
            List of audit event dictionaries
        """
        query = "SELECT * FROM audit_log WHERE 1=1"
        params = []

        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)

        if actor_id:
            query += " AND actor_id = ?"
            params.append(actor_id)

        if target_id:
            query += " AND target_id = ?"
            params.append(target_id)

        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        return await self.db.fetch_all(query, tuple(params))
