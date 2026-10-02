"""A tiny thread-safe TTL cache, one per data class (071 §3.2).

Data classes and their lifetimes:

* ``price``     — 20 s. A price is a moving fact; 20 s spares a holdings read
  and the guard that follows it a second round of identical calls.
* ``screen``    — 60 s. One GoPlus answer serves ``screen`` AND ``holders``
  for the same token (R8: it was fetched twice per ``token_info``).
* ``immutable`` — forever (decimals, a deployed contract's code).

The clock is injectable so tests never sleep. Only ANSWERS are cached: a
caller must not put a failure in, because a cached failure is an outage that
outlives the outage.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Hashable, Optional, Tuple

TTL_SEC = {"price": 20.0, "screen": 60.0, "immutable": None}

_MISSING = object()


class TTLCache:
    def __init__(self, ttl: Optional[float], *, clock: Callable[[], float] = time.monotonic,
                 max_items: int = 4096) -> None:
        self.ttl = ttl
        self._clock = clock
        self._max = max_items
        self._data: Dict[Hashable, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Hashable, default: Any = None) -> Any:
        hit = self.get_with_age(key)
        return default if hit is None else hit[0]

    def get_with_age(self, key: Hashable) -> Optional[Tuple[Any, float]]:
        """``(value, age_seconds)`` or ``None`` when absent or expired."""
        now = self._clock()
        with self._lock:
            item = self._data.get(key, _MISSING)
            if item is _MISSING:
                return None
            stored_at, value = item
            age = now - stored_at
            if self.ttl is not None and age > self.ttl:
                del self._data[key]
                return None
            return value, max(age, 0.0)

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            if len(self._data) >= self._max and key not in self._data:
                # Drop the oldest entry; bounded memory beats a perfect LRU here.
                oldest = min(self._data.items(), key=lambda kv: kv[1][0])[0]
                del self._data[oldest]
            self._data[key] = (self._clock(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


_CACHES: Dict[str, TTLCache] = {}
_CACHES_LOCK = threading.Lock()


def cache_for(data_class: str) -> TTLCache:
    """The process-wide cache for *data_class* (see ``TTL_SEC``)."""
    with _CACHES_LOCK:
        c = _CACHES.get(data_class)
        if c is None:
            if data_class not in TTL_SEC:
                raise KeyError(f"unknown data class {data_class!r}")
            c = _CACHES[data_class] = TTLCache(TTL_SEC[data_class])
        return c


def clear_all() -> None:
    """Empty every cache (tests; never needed in production)."""
    with _CACHES_LOCK:
        caches = list(_CACHES.values())
    for c in caches:
        c.clear()
