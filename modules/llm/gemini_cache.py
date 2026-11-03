"""Gemini explicit ``cachedContents`` leases (UP-08; 098 MO-14).

A cached content object is a BILLED remote resource with a server-side TTL. It
is held as a lease, not a plain field:

- key = ``(model_type, system-prompt digest, tool signature)`` — the object bakes
  in the model, the system instruction AND the tools, so all three key it;
- at most :data:`MAX_LEASES` handles (LRU), so two sessions with different
  system prompts on one shared client keep two handles instead of deleting each
  other's object (and failing the other's in-flight request);
- ``expires_at = created + ttl - 30 s``: an expired handle is rebuilt, never used;
- a request that fails WITH a cache drops that handle (the caller retries once
  uncached); ``clear()`` deletes every handle (client cleanup).
"""
from __future__ import annotations

import hashlib
import os
import time
from collections import OrderedDict
from typing import Any, Callable, Optional, Tuple

MAX_LEASES = 4
EXPIRY_MARGIN_SEC = 30.0

Key = Tuple[str, str, str]


def tool_signature(tool_objects) -> str:
    """Stable signature of the tool schema."""
    names = []
    for t in (tool_objects or []):
        for f in getattr(t, 'function_declarations', []) or []:
            names.append(getattr(f, 'name', ''))
    return ",".join(sorted(names))


def cache_key(model_type: str, system_instruction, tool_objects) -> Key:
    sys_hash = hashlib.sha256((system_instruction or "").encode("utf-8")).hexdigest()[:16]
    return (str(model_type or ""), sys_hash, tool_signature(tool_objects))


def _delete(cache: Any, logger=None) -> None:
    try:
        cache.delete()
    except Exception as e:
        if logger is not None:
            logger.debug(f"Gemini cache delete failed (ignored): {e}")


class CacheLeases:
    def __init__(self, clock: Callable[[], float] = time.monotonic, logger=None) -> None:
        self._items: "OrderedDict[Key, Tuple[Any, float]]" = OrderedDict()
        self._clock = clock
        self.logger = logger

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: Key) -> Optional[Any]:
        item = self._items.get(key)
        if item is None:
            return None
        cache, expires_at = item
        if self._clock() >= expires_at:  # the server is about to delete it
            self._items.pop(key, None)
            _delete(cache, self.logger)
            return None
        self._items.move_to_end(key)
        return cache

    def put(self, key: Key, cache: Any, ttl_sec: float) -> None:
        self._items[key] = (cache, self._clock() + max(0.0, ttl_sec - EXPIRY_MARGIN_SEC))
        self._items.move_to_end(key)
        while len(self._items) > MAX_LEASES:
            _, (old, _) = self._items.popitem(last=False)
            _delete(old, self.logger)

    def drop(self, cache: Any) -> None:
        """A request with ``cache`` failed: forget (and delete) that handle."""
        for k, (c, _) in list(self._items.items()):
            if c is cache:
                self._items.pop(k, None)
                _delete(c, self.logger)

    def clear(self) -> None:
        items, self._items = list(self._items.values()), OrderedDict()
        for c, _ in items:
            _delete(c, self.logger)


def leases_of(client) -> CacheLeases:
    leases = getattr(client, "_cache_leases", None)
    if leases is None:
        leases = CacheLeases(logger=getattr(client, "logger", None))
        client._cache_leases = leases
    return leases


def build_or_reuse(client, system_instruction, tool_objects, *, min_tokens: int):
    """A reusable CachedContent for the stable system+tools prefix, or None.
    The caller checks the flag (``GEMINI_PROMPT_CACHE``, default OFF); scoped to
    the non-Gemini-3 path; fail-open to the uncached path on any SDK error.
    Imports nothing from the LLM package (kept out of the import cycle)."""
    if client._is_gemini_3_model():
        return None
    est_chars = len(system_instruction or "") + len(tool_signature(tool_objects))
    if est_chars // 4 < min_tokens:
        return None
    leases = leases_of(client)
    key = cache_key(client.model_type, system_instruction, tool_objects)
    cache = leases.get(key)
    if cache is not None:
        return cache
    try:
        import datetime
        import google.generativeai as genai
        ttl_min = int(os.getenv("GEMINI_CACHE_TTL_MIN", "10"))
        cache = genai.caching.CachedContent.create(
            model=client.model_type,
            system_instruction=system_instruction or None,
            tools=tool_objects or None,
            ttl=datetime.timedelta(minutes=ttl_min),
        )
        leases.put(key, cache, ttl_min * 60.0)
        client.logger.info(
            f"event=gemini_explicit_cache_created model={client.model_type} "
            f"tools={key[2].count(',') + 1 if key[2] else 0} ttl_min={ttl_min}")
        return cache
    except Exception as e:
        client.logger.warning(f"Gemini explicit cache create failed, continuing uncached: {e}")
        return None
