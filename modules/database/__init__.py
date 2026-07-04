# modules/database/__init__.py

"""Database module for managing all database operations."""

from typing import Optional

# Import base components first
from .connection import DatabaseConnection

# Import table handlers
from .conversation_contexts import ConversationContexts
from .user_profiles import UserProfiles
from .auth_tables import AuthTables
from .x402_tables import X402Tables

# Import error classes
class DatabaseInitError(Exception):
    """Raised when database initialization fails."""
    pass

# Note: DatabaseManager import moved to end to avoid circular imports
__all__ = [
    'DatabaseManager',
    'DatabaseConnection',
    'ConversationContexts',
    'UserProfiles',
    'AuthTables',
    'X402Tables',
    'DatabaseInitError'
]

# Import manager last to avoid circular imports
from .database_manager import DatabaseManager
