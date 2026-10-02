"""Cache manager implementation."""

from collections import OrderedDict
from typing import Any, Optional, Dict
import asyncio
import json
import time

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
        # key -> monotonic deadline; only keys set with a ttl have a row here.
        # 2026-09-22 (harness/cache review F27): every ``set(..., ttl=)`` caller
        # raised TypeError before this existed — MCP read_resource turned each
        # successful read into a ToolError.
        self._expiry: Dict[str, float] = {}
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
            self._expiry.clear()
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
            self._expiry.clear()
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
                    deadline = self._expiry.get(key)
                    if deadline is not None and time.monotonic() >= deadline:
                        del self._cache[key]
                        del self._expiry[key]
                        self.logger.debug(f"Cache expired for key: {key}")
                        return None
                    value = self._cache[key]
                    self._cache.move_to_end(key)  # Move to end for LRU
                    self.logger.debug(f"Cache hit for key: {key}")
                    return value
                    
                self.logger.debug(f"Cache miss for key: {key}")
                return None
            except Exception as e:
                self.logger.error(f"Error retrieving from cache: {e}")
                return None

    async def set(self, key: str, value: Any, *, ttl: Optional[float] = None) -> None:
        """Set an item in the cache.

        *ttl* (seconds, optional) makes the entry expire on read; ``None`` (the
        default) keeps the LRU-only lifetime every 2-arg caller relies on.
        """
        if not self._initialized:
            await self.initialize()
            
        async with self._lock:
            try:
                # Update cache
                if key in self._cache:
                    self._cache.move_to_end(key)
                self._cache[key] = value
                if ttl is not None and ttl > 0:
                    self._expiry[key] = time.monotonic() + float(ttl)
                else:
                    self._expiry.pop(key, None)
                
                # Enforce size limit (LRU eviction)
                while len(self._cache) > self.max_size:
                    oldest_key, _ = self._cache.popitem(last=False)
                    self._expiry.pop(oldest_key, None)
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
                    self._expiry.pop(key, None)
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
                self._expiry.clear()
                self.logger.debug("Cache cleared")
            except Exception as e:
                self.logger.error(f"Error clearing cache: {e}")
                raise ModuleError(f"Failed to clear cache: {e}")

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