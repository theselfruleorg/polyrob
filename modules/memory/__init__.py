"""Memory management module."""

# Import models first
from .models import (
    Message,
    MessageRole,
    ConversationContext,
    UserProfile,
    KnowledgeEntry,
)

# Import managers
from .cache_manager import CacheManager

# Import task memory system (Phase 1 - PRIMARY)
from .task.task_context_manager import TaskContextManager

# Import memory manager last to avoid circular imports
from .memory_manager import MemoryManager

__all__ = [
    # Managers
    'MemoryManager',
    'CacheManager',
    'TaskContextManager',

    # Models
    'Message',
    'MessageRole',
    'ConversationContext',
    'UserProfile',
    'KnowledgeEntry'
]
