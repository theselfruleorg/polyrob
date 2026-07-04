"""Bounded collection utilities to prevent memory leaks."""

from collections import OrderedDict
from typing import Any, Dict, Optional, TypeVar, Generic
import logging
from datetime import datetime, timedelta

K = TypeVar('K')
V = TypeVar('V')

logger = logging.getLogger(__name__)


class BoundedDict(Generic[K, V]):
    """Dictionary with a maximum size that uses LRU eviction."""

    def __init__(self, max_size: int = 1000):
        """Initialize bounded dictionary.

        Args:
            max_size: Maximum number of items to store
        """
        self.max_size = max_size
        self._data: OrderedDict[K, V] = OrderedDict()
        self._evicted_count = 0

    def __setitem__(self, key: K, value: V) -> None:
        """Set an item, evicting oldest if at capacity."""
        if key in self._data:
            # Move to end (most recently used)
            self._data.move_to_end(key)
        else:
            # Add new item
            if len(self._data) >= self.max_size:
                # Evict oldest
                evicted = self._data.popitem(last=False)
                self._evicted_count += 1
                logger.debug(f"Evicted item {evicted[0]} from bounded dict (total evicted: {self._evicted_count})")

        self._data[key] = value

    def __getitem__(self, key: K) -> V:
        """Get an item and mark it as recently used."""
        value = self._data[key]
        self._data.move_to_end(key)
        return value

    def __delitem__(self, key: K) -> None:
        """Delete an item."""
        del self._data[key]

    def __contains__(self, key: K) -> bool:
        """Check if key exists."""
        return key in self._data

    def __len__(self) -> int:
        """Return number of items."""
        return len(self._data)

    def get(self, key: K, default: Optional[V] = None) -> Optional[V]:
        """Get an item with default value."""
        if key in self._data:
            return self[key]
        return default

    def pop(self, key: K, default: Optional[V] = None) -> Optional[V]:
        """Remove and return an item."""
        return self._data.pop(key, default)

    def clear(self) -> None:
        """Clear all items."""
        self._data.clear()

    def items(self):
        """Return items view."""
        return self._data.items()

    def keys(self):
        """Return keys view."""
        return self._data.keys()

    def values(self):
        """Return values view."""
        return self._data.values()

    def cleanup_old(self, max_age: timedelta) -> int:
        """Remove items older than max_age if they have a timestamp attribute.

        Args:
            max_age: Maximum age for items

        Returns:
            Number of items removed
        """
        now = datetime.now()
        cutoff = now - max_age
        removed = 0

        keys_to_remove = []
        for key, value in self._data.items():
            if hasattr(value, 'timestamp') and value.timestamp < cutoff:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._data[key]
            removed += 1

        if removed > 0:
            logger.info(f"Cleaned up {removed} old items from bounded dict")

        return removed


class BoundedSet(Generic[V]):
    """Set with a maximum size that uses FIFO eviction."""

    def __init__(self, max_size: int = 10000):
        """Initialize bounded set.

        Args:
            max_size: Maximum number of items to store
        """
        self.max_size = max_size
        self._data: OrderedDict[V, None] = OrderedDict()
        self._evicted_count = 0

    def add(self, item: V) -> None:
        """Add an item, evicting oldest if at capacity."""
        if item in self._data:
            return

        if len(self._data) >= self.max_size:
            # Evict oldest
            evicted = self._data.popitem(last=False)
            self._evicted_count += 1
            logger.debug(f"Evicted item {evicted[0]} from bounded set (total evicted: {self._evicted_count})")

        self._data[item] = None

    def remove(self, item: V) -> None:
        """Remove an item."""
        del self._data[item]

    def discard(self, item: V) -> None:
        """Remove an item if present."""
        self._data.pop(item, None)

    def __contains__(self, item: V) -> bool:
        """Check if item exists."""
        return item in self._data

    def __len__(self) -> int:
        """Return number of items."""
        return len(self._data)

    def clear(self) -> None:
        """Clear all items."""
        self._data.clear()

    def __iter__(self):
        """Iterate over items."""
        return iter(self._data.keys())