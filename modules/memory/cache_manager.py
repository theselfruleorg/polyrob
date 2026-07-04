"""Cache manager implementation."""

from collections import OrderedDict
from typing import Any, Optional, Dict
import asyncio
import json

from modules.base_module import BaseModule
from core.config import BotConfig
from core.exceptions import ModuleError

class CacheManager(BaseModule):
    """Manages in-memory caching for memory components."""
    
    @property
    def required_modules(self) -> Dict[str, str]:
        """Get required modules."""
        return {}

    @property
    def optional_modules(self) -> Dict[str, str]:
        """Get optional modules."""
        return {}

    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        """Initialize cache manager."""
        super().__init__(name=name, config=config, container=container)
        self._cache = OrderedDict()  # Use OrderedDict for LRU functionality
        self.max_size = getattr(config, 'cache_size', 1000)
        self.logger.info(f"Cache manager initialized with max size: {self.max_size}")

    def _validate_dependencies(self) -> None:
        """Validate dependencies."""
        pass  # No dependencies to validate

    async def _initialize(self) -> None:
        """Initialize cache manager."""
        try:
            self.logger.info("Starting Cache Manager initialization")
            self._cache.clear()  # Ensure clean state
            self._initialized = True
            self.logger.info("Cache Manager initialization completed")
        except Exception as e:
            self._initialized = False
            self.logger.error(f"Cache Manager initialization failed: {e}")
            raise ModuleError(f"Failed to initialize cache manager: {e}")

    async def _cleanup(self) -> None:
        """Clean up cache manager resources."""
        try:
            self.logger.info("Starting Cache Manager cleanup")
            self._cache.clear()
            self._initialized = False
            self.logger.info("Cache Manager cleanup completed")
        except Exception as e:
            self.logger.error(f"Cache Manager cleanup failed: {e}")
            raise ModuleError(f"Failed to clean up cache manager: {e}")

    async def get(self, key: str) -> Optional[Any]:
        """Retrieve an item from the cache."""
        if not self._initialized:
            await self.initialize()
            
        async with self._lock:
            try:
                if key in self._cache:
                    value = self._cache[key]
                    self._cache.move_to_end(key)  # Move to end for LRU
                    self.logger.debug(f"Cache hit for key: {key}")
                    return value
                    
                self.logger.debug(f"Cache miss for key: {key}")
                return None
            except Exception as e:
                self.logger.error(f"Error retrieving from cache: {e}")
                return None

    async def set(self, key: str, value: Any) -> None:
        """Set an item in the cache."""
        if not self._initialized:
            await self.initialize()
            
        async with self._lock:
            try:
                # Update cache
                if key in self._cache:
                    self._cache.move_to_end(key)
                self._cache[key] = value
                
                # Enforce size limit (LRU eviction)
                while len(self._cache) > self.max_size:
                    oldest_key, _ = self._cache.popitem(last=False)
                    self.logger.debug(f"Cache evicted key: {oldest_key}")
                    
                self.logger.debug(f"Cache set for key: {key}")
            except Exception as e:
                self.logger.error(f"Error setting cache value: {e}")
                raise ModuleError(f"Failed to set cache value: {e}")

    async def delete(self, key: str) -> None:
        """Delete an item from the cache."""
        if not self._initialized:
            await self.initialize()
            
        async with self._lock:
            try:
                if key in self._cache:
                    del self._cache[key]
                    self.logger.debug(f"Cache deleted key: {key}")
            except Exception as e:
                self.logger.error(f"Error deleting from cache: {e}")
                raise ModuleError(f"Failed to delete from cache: {e}")

    async def clear(self) -> None:
        """Clear all items from the cache."""
        if not self._initialized:
            await self.initialize()
            
        async with self._lock:
            try:
                self._cache.clear()
                self.logger.debug("Cache cleared")
            except Exception as e:
                self.logger.error(f"Error clearing cache: {e}")
                raise ModuleError(f"Failed to clear cache: {e}")

    def get_size(self) -> int:
        """Get current cache size."""
        return len(self._cache)

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return {
            'size': len(self._cache),
            'max_size': self.max_size,
            'usage': len(self._cache) / self.max_size * 100 if self.max_size > 0 else 0,
            'initialized': self._initialized
        }

    async def serialize(self, value: Any) -> str:
        """Serialize value for caching."""
        try:
            return json.dumps(value)
        except Exception as e:
            self.logger.error(f"Error serializing value: {e}")
            raise ModuleError(f"Failed to serialize value: {e}")

    async def deserialize(self, value: str) -> Any:
        """Deserialize cached value."""
        try:
            return json.loads(value)
        except Exception as e:
            self.logger.error(f"Error deserializing value: {e}")
            raise ModuleError(f"Failed to deserialize value: {e}")

    async def clear_user_data(self, user_id: str) -> None:
        """Clear all cached data for a specific user.
        
        Args:
            user_id: User ID to clear cache for
            
        Raises:
            ModuleError: If clearing cache fails
        """
        if not self._initialized:
            await self.initialize()
            
        async with self._lock:
            try:
                # Find all keys related to this user
                user_keys = [
                    key for key in self._cache.keys()
                    if str(user_id) in key
                ]
                
                # Delete all user-related keys
                for key in user_keys:
                    if key in self._cache:
                        del self._cache[key]
                        self.logger.debug(f"Cleared cache for key: {key}")
                
                self.logger.info(f"Cleared all cached data for user {user_id}")
                
            except Exception as e:
                self.logger.error(f"Error clearing user cache data: {e}")
                raise ModuleError(f"Failed to clear user cache data: {str(e)}")