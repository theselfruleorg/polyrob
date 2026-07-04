"""Memory manager implementation."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Dict, Any, List, Union

from core.config import BotConfig
from core.exceptions import MemoryError, ModuleError, DependencyError
from modules.base_module import BaseModule

# Import managers
from .cache_manager import CacheManager
from .user_profile_manager import UserProfileManager
from .task.task_context_manager import TaskContextManager


class MemoryManager(BaseModule):
    """Central memory storage coordinator."""
    
    @property
    def required_modules(self) -> Dict[str, str]:
        """Get required modules."""
        return {
            'database_manager': 'Database management module'
        }

    @property
    def optional_modules(self) -> Dict[str, str]:
        """Get optional modules."""
        return {
            'llm_client': 'LLM service for text generation',
            'embedding_model': 'Embedding model for text vectorization',
            'vector_storage': 'Vector storage for embeddings'
        }

    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        """Initialize memory manager."""
        super().__init__(name=name, config=config, container=container)
        self.cache = None
        self.user_profile_manager = None
        self.task_context_manager = None
        self._knowledge_base = None
        self._database = None

    def _validate_dependencies(self) -> None:
        """Validate required dependencies."""
        if not self.container:
            raise ModuleError("Container not provided")
        
        self._database = self.container.get_service('database_manager')
        if not self._database:
            raise ModuleError("Database service not available")

    async def _initialize(self) -> None:
        """Initialize memory manager."""
        try:
            self.logger.info("Starting Memory Manager initialization")
            
            # Validate dependencies first
            self._validate_dependencies()
            
            # Get or create cache manager
            if self.container and self.container.has_service('cache_manager'):
                # Reuse existing cache manager
                self.cache = self.container.get_service('cache_manager')
                self.logger.info("Using existing Cache Manager")
            else:
                # Create new cache manager
                self.cache = CacheManager(
                    name="cache_manager",
                    config=self.config
                )
                await self.cache.initialize()
                # Register with container if possible
                if self.container:
                    self.container.register_service('cache_manager', self.cache)
                self.logger.info("Created and initialized Cache Manager")
            
            # RAG knowledge base retired: cross-session semantic recall now lives in the
            # local sqlite-vec memory backend (LocalVectorMemoryProvider). knowledge_base
            # stays None; callers (e.g. ChatAgent) already guard for it.
            self._knowledge_base = None

            # Initialize user profile manager
            self.user_profile_manager = UserProfileManager(
                name="user_profile_manager",
                config=self.config,
                database=self._database,
                cache=self.cache
            )
            await self.user_profile_manager.initialize()
            self.logger.info("User Profile Manager initialized")

            # Initialize task context manager (Phase 1 hierarchical memory - PRIMARY)
            self.task_context_manager = TaskContextManager(
                name="task_context_manager",
                config=self.config
            )
            await self.task_context_manager.initialize()
            self.logger.info("Task Context Manager initialized")

            self.logger.info("Memory Manager initialization completed")
            
        except Exception as e:
            self.logger.error(f"Memory Manager initialization failed: {e}")
            raise ModuleError(f"Failed to initialize memory manager: {e}")

    async def _cleanup(self) -> None:
        """Clean up memory manager resources."""
        try:
            self.logger.info("Starting Memory Manager cleanup")
            
            if self._knowledge_base:
                await self._knowledge_base.cleanup()
                self.logger.info("Knowledge Base cleaned up")
            
            if self.cache:
                await self.cache.cleanup()
                self.logger.info("Cache Manager cleaned up")

            if self.user_profile_manager:
                await self.user_profile_manager.cleanup()
                self.logger.info("User Profile Manager cleaned up")

            if self.task_context_manager:
                await self.task_context_manager.cleanup()
                self.logger.info("Task Context Manager cleaned up")

            self.logger.info("Memory Manager cleanup completed")
            
        except Exception as e:
            self.logger.error(f"Memory Manager cleanup failed: {e}")
            raise ModuleError(f"Failed to clean up memory manager: {e}")

    def get_module(self, name: str) -> Optional[Any]:
        """Get a module by name."""
        if self.container:
            return self.container.get_module(name)
        return None

    @property
    def database(self):
        """Get database module."""
        return self._database

    @property
    def embedding_model(self):
        """Get embedding model."""
        return self.container.get_service('embedding_model') if self.container else None

    @property
    def vector_storage(self):
        """Get vector storage."""
        return self.container.get_service('vector_storage') if self.container else None

    @property
    def knowledge_base(self):
        """Retired: the RAG knowledge base was removed in favour of the local
        sqlite-vec memory backend. Always None; retained so existing guards
        (`if self.memory_manager.knowledge_base`) keep working."""
        return None

    def set_knowledge_base(self, knowledge_base) -> None:
        """No-op shim kept for back-compat; the RAG knowledge base was retired."""
        self.logger.debug("set_knowledge_base ignored (RAG knowledge base retired)")

    @property
    def user_profiles(self):
        """Get user profiles."""
        return self.database.user_profiles if self.database else None

    @property
    def required_dependencies(self) -> List[str]:
        """Get required dependencies."""
        return ['database_manager']

    async def _initialize_dependencies(self) -> None:
        """Initialize dependencies for the memory manager."""
        try:
            # Initialize dependencies
            await self._initialize()

            self.logger.info("All dependencies initialized successfully")

        except Exception as e:
            self.logger.error(f"Failed to initialize dependencies: {e}")
            raise ModuleError(f"Failed to initialize dependencies: {e}")

    async def _cleanup_dependencies(self) -> None:
        """Clean up dependencies for the memory manager."""
        try:
            await self._cleanup()

            self.logger.info("All dependencies cleaned up successfully")

        except Exception as e:
            self.logger.error(f"Failed to clean up dependencies: {e}")
            raise ModuleError(f"Failed to clean up dependencies: {e}")

    async def clear_memory(self, user_id: str) -> None:
        """Clear all memory data for a user.
        
        Args:
            user_id: User ID to clear memory for
            
        Raises:
            MemoryError: If clearing memory fails
        """
        try:
            # Clear cache
            if self.cache:
                await self.cache.clear_user_data(user_id)

            # Clear user profile cache
            if self.user_profile_manager:
                await self.user_profile_manager.clear_cache(user_id)
            
            # Note: Skipping knowledge base context clearing as it's not critical
            # and requires additional implementation

            self.logger.info(f"Cleared all memory data for user {user_id}")

        except Exception as e:
            self.logger.error(f"Failed to clear memory for user {user_id}: {e}")
            raise MemoryError(f"Failed to clear memory: {str(e)}") 