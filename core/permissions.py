"""Location: core/permissions.py"""

"""Permissions management - Platform agnostic version.

LEGACY: This module dates from the Telegram era and assumes integer user IDs
and a multi-user role DB. Wallet-based auth (rob-server) does its own checks
via api.auth_constants/core.constants and does not rely on this class.

For the rob-core split (PR3, cut C) the eager `from modules.memory...` import
was moved behind TYPE_CHECKING so this module is core-import-clean. The
runtime behavior is preserved: when a MemoryManager is wired in (server mode)
the DB-backed role lookups work; in core mode the manager is None and the
checks silently fall back to in-memory state.

Future work (server repo split): the DB-backed branches move to rob-server's
permission policy module; this file shrinks to a single-user, always-True
permission shim for rob-core.
"""

import logging
from typing import TYPE_CHECKING, Dict, Optional, Set, Any, List, Union
import asyncio
from functools import wraps

from core.logging import get_component_logger
from core.exceptions import PermissionsError
from core.base_component import BaseComponent
from core.config import BotConfig

if TYPE_CHECKING:
    # Type-only import to avoid pulling modules.memory at import time.
    # That coupling was the reason `import core` eagerly loaded the entire
    # memory subsystem — see CLAUDE.md note about pre-existing circulars.
    from modules.memory.memory_manager import MemoryManager

logger = get_component_logger('permissions', log_file='permissions.log')

# Define permitted roles as a constant
# NOTE: This is LEGACY code from Telegram-era. New wallet-based auth uses core.constants
# SIMPLIFIED: Only user and admin roles now (moderator/super_admin removed)
PERMITTED_ROLES = {'user', 'admin'}

class Permissions(BaseComponent):
    """Manages user permissions and roles - platform agnostic."""

    def __init__(self, config: BotConfig, memory: Optional["MemoryManager"] = None):
        """Initialize permissions management."""
        super().__init__(name="permissions", config=config)
        self.config = config
        self.memory = memory
        self.admin_ids: Set[int] = set()
        self.super_admin_ids: Set[int] = set()
        self.moderator_ids: Set[int] = set()
        self.blocked_users: Set[int] = set()
        self.whitelisted_chats: Set[int] = set()

    async def _initialize(self) -> None:
        """Initialize the permissions system."""
        logger.info("Initializing permissions system")

        # Load permissions from database if available
        if self.memory and hasattr(self.memory, 'database_manager'):
            try:
                db = self.memory.database_manager

                # Load super admin IDs
                super_admin_users = await db.execute_query(
                    "SELECT user_id FROM user_profiles WHERE role = 'super_admin'"
                )
                for row in super_admin_users:
                    self.super_admin_ids.add(int(row[0]))

                # Load admin IDs (including super_admins for backwards compatibility)
                admin_users = await db.execute_query(
                    "SELECT user_id FROM user_profiles WHERE role IN ('admin', 'super_admin')"
                )
                for row in admin_users:
                    self.admin_ids.add(int(row[0]))

                # Load moderator IDs
                mod_users = await db.execute_query(
                    "SELECT user_id FROM user_profiles WHERE role = 'moderator'"
                )
                for row in mod_users:
                    self.moderator_ids.add(int(row[0]))

                # Load blocked users
                blocked = await db.execute_query(
                    "SELECT user_id FROM blocked_users WHERE is_blocked = 1"
                )
                for row in blocked:
                    self.blocked_users.add(int(row[0]))

                logger.info(
                    f"Loaded permissions - Super Admins: {len(self.super_admin_ids)}, "
                    f"Admins: {len(self.admin_ids)}, "
                    f"Moderators: {len(self.moderator_ids)}, "
                    f"Blocked: {len(self.blocked_users)}"
                )
            except Exception as e:
                logger.error(f"Error loading permissions from database: {e}")

    async def _cleanup(self) -> None:
        """Clean up permissions resources."""
        logger.info("Cleaning up permissions")

    # User role checks
    async def is_super_admin(self, user_id: int) -> bool:
        """Check if user is super admin."""
        return user_id in self.super_admin_ids

    async def is_admin(self, user_id: int) -> bool:
        """Check if user is admin or super admin."""
        return user_id in self.admin_ids or await self.is_super_admin(user_id)

    async def is_moderator(self, user_id: int) -> bool:
        """Check if user is moderator or higher."""
        return user_id in self.moderator_ids or await self.is_admin(user_id)

    async def is_blocked(self, user_id: int) -> bool:
        """Check if user is blocked."""
        return user_id in self.blocked_users

    async def is_whitelisted_chat(self, chat_id: int) -> bool:
        """Check if chat is whitelisted."""
        return chat_id in self.whitelisted_chats

    # User management
    async def add_admin(self, user_id: int) -> bool:
        """Add user as admin."""
        self.admin_ids.add(user_id)
        logger.info(f"Added admin: {user_id}")

        # Update database if available
        if self.memory and hasattr(self.memory, 'database_manager'):
            try:
                await self.memory.database_manager.execute_query(
                    "UPDATE user_profiles SET role = 'admin' WHERE user_id = ?",
                    (user_id,)
                )
            except Exception as e:
                logger.error(f"Error updating admin in database: {e}")

        return True

    async def remove_admin(self, user_id: int) -> bool:
        """Remove user as admin."""
        if await self.is_super_admin(user_id):
            raise PermissionsError("Cannot remove super admin")

        self.admin_ids.discard(user_id)
        logger.info(f"Removed admin: {user_id}")

        # Update database if available
        if self.memory and hasattr(self.memory, 'database_manager'):
            try:
                await self.memory.database_manager.execute_query(
                    "UPDATE user_profiles SET role = 'user' WHERE user_id = ?",
                    (user_id,)
                )
            except Exception as e:
                logger.error(f"Error updating admin in database: {e}")

        return True

    async def add_moderator(self, user_id: int) -> bool:
        """Add user as moderator."""
        self.moderator_ids.add(user_id)
        logger.info(f"Added moderator: {user_id}")

        # Update database if available
        if self.memory and hasattr(self.memory, 'database_manager'):
            try:
                await self.memory.database_manager.execute_query(
                    "UPDATE user_profiles SET role = 'moderator' WHERE user_id = ?",
                    (user_id,)
                )
            except Exception as e:
                logger.error(f"Error updating moderator in database: {e}")

        return True

    async def remove_moderator(self, user_id: int) -> bool:
        """Remove user as moderator."""
        self.moderator_ids.discard(user_id)
        logger.info(f"Removed moderator: {user_id}")

        # Update database if available
        if self.memory and hasattr(self.memory, 'database_manager'):
            try:
                await self.memory.database_manager.execute_query(
                    "UPDATE user_profiles SET role = 'user' WHERE user_id = ?",
                    (user_id,)
                )
            except Exception as e:
                logger.error(f"Error updating moderator in database: {e}")

        return True

    async def block_user(self, user_id: int, reason: Optional[str] = None) -> bool:
        """Block a user."""
        if await self.is_super_admin(user_id):
            raise PermissionsError("Cannot block super admin")

        self.blocked_users.add(user_id)
        logger.info(f"Blocked user: {user_id}, reason: {reason}")

        # Update database if available
        if self.memory and hasattr(self.memory, 'database_manager'):
            try:
                await self.memory.database_manager.execute_query(
                    """
                    INSERT OR REPLACE INTO blocked_users (user_id, is_blocked, blocked_reason)
                    VALUES (?, 1, ?)
                    """,
                    (user_id, reason)
                )
            except Exception as e:
                logger.error(f"Error blocking user in database: {e}")

        return True

    async def unblock_user(self, user_id: int) -> bool:
        """Unblock a user."""
        self.blocked_users.discard(user_id)
        logger.info(f"Unblocked user: {user_id}")

        # Update database if available
        if self.memory and hasattr(self.memory, 'database_manager'):
            try:
                await self.memory.database_manager.execute_query(
                    "UPDATE blocked_users SET is_blocked = 0 WHERE user_id = ?",
                    (user_id,)
                )
            except Exception as e:
                logger.error(f"Error unblocking user in database: {e}")

        return True

    async def whitelist_chat(self, chat_id: int) -> bool:
        """Add chat to whitelist."""
        self.whitelisted_chats.add(chat_id)
        logger.info(f"Whitelisted chat: {chat_id}")
        return True

    async def remove_from_whitelist(self, chat_id: int) -> bool:
        """Remove chat from whitelist."""
        self.whitelisted_chats.discard(chat_id)
        logger.info(f"Removed from whitelist: {chat_id}")
        return True

    async def get_user_role(self, user_id: int) -> str:
        """Get user's role."""
        if await self.is_super_admin(user_id):
            return 'super_admin'
        elif user_id in self.admin_ids:
            return 'admin'
        elif user_id in self.moderator_ids:
            return 'moderator'
        else:
            return 'user'

    async def check_permission(
        self,
        user_id: int,
        required_role: str = 'user',
        chat_id: Optional[int] = None
    ) -> bool:
        """Check if user has required permission level."""
        if await self.is_blocked(user_id):
            return False

        # Check chat whitelist if applicable
        if chat_id and not await self.is_whitelisted_chat(chat_id):
            # If chat is not whitelisted, only allow admins
            if not await self.is_admin(user_id):
                return False

        # Check role hierarchy (simplified: only user and admin)
        user_role = await self.get_user_role(user_id)
        role_hierarchy = {
            'user': 0,
            'admin': 1
        }

        user_level = role_hierarchy.get(user_role, 0)
        required_level = role_hierarchy.get(required_role, 0)

        return user_level >= required_level

    def get_stats(self) -> Dict[str, Any]:
        """Get permission statistics."""
        return {
            'admin_count': len(self.admin_ids),
            'blocked_count': len(self.blocked_users),
            'whitelisted_chats': len(self.whitelisted_chats)
        }


# Platform-agnostic permission decorator
def require_permission(role: str = 'user'):
    """Decorator to require certain permission level."""
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Try to extract user_id from various sources
            user_id = None

            # Check kwargs
            if 'user_id' in kwargs:
                user_id = kwargs['user_id']
            # Check if first arg is user_id (common pattern)
            elif args and isinstance(args[0], int):
                user_id = args[0]

            if user_id and hasattr(self, 'permissions'):
                if not await self.permissions.check_permission(user_id, role):
                    raise PermissionsError(f"User {user_id} lacks required permission: {role}")

            return await func(self, *args, **kwargs)
        return wrapper
    return decorator