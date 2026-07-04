"""Process-global memory-provider registry + agent-facing helpers (roadmap P7).

Makes the P7 ``MemoryProviderRegistry`` a live, consumable singleton and gives the
agent two no-throw entry points (``memory_prefetch`` / ``memory_sync_turn``) that
route through the *active* provider. The default active provider is
``NullMemoryProvider`` — so when no external backend is configured these are
no-ops and production behavior is unchanged. Registering an external provider
(``set_external_memory_provider``) makes the live agent start using it.
"""
from __future__ import annotations

import logging
from typing import Optional

from modules.memory.provider import (
    MemoryProvider, NullMemoryProvider, MemoryProviderRegistry,
)

logger = logging.getLogger(__name__)

_registry: Optional[MemoryProviderRegistry] = None


def get_memory_registry() -> MemoryProviderRegistry:
    """Return the process-global registry, seeded with a Null provider on first use."""
    global _registry
    if _registry is None:
        _registry = MemoryProviderRegistry()
        _registry.register(NullMemoryProvider())
    return _registry


def set_external_memory_provider(provider: MemoryProvider) -> MemoryProvider:
    """Register the single external memory provider (one-provider constraint enforced)."""
    return get_memory_registry().register(provider)


def reset_memory_registry() -> None:
    """Test/teardown helper: drop all providers (next get_* re-seeds Null)."""
    global _registry
    _registry = None


async def memory_prefetch(query: str, *, session_id: str, user_id: Optional[str] = None) -> str:
    """Route a prefetch through the active provider; '' for Null / on any error.

    ``user_id`` scopes recall to the tenant (P0-0) so one user's memory never leaks
    into another's recall in a multi-tenant deployment.
    """
    provider = get_memory_registry().active()
    if provider is None:
        return ""
    try:
        return await provider.prefetch(query, session_id=session_id, user_id=user_id)
    except Exception as e:  # never break the agent loop on a memory backend hiccup
        logger.warning("memory_prefetch failed: %s", e)
        return ""


async def memory_search(query: str, *, session_id: str = "", user_id: Optional[str] = None,
                        limit: int = 5, sort: Optional[str] = None) -> str:
    """Route an agent-initiated recall (discover/browse) through the active provider.

    Unlike ``memory_prefetch`` (automatic, fixed top_k, rank-only), this exposes the
    provider's richer ``search`` (bounded limit, sort, browse-on-empty). '' for Null
    / on any error. Tenant-scoped via ``user_id`` exactly like prefetch.
    """
    provider = get_memory_registry().active()
    if provider is None:
        return ""
    try:
        return await provider.search(query, user_id=user_id, session_id=session_id,
                                     limit=limit, sort=sort)
    except Exception as e:
        logger.warning("memory_search failed: %s", e)
        return ""


async def memory_sync_turn(user_content: str, assistant_content: str, *,
                           session_id: str, user_id: Optional[str] = None) -> None:
    """Route a completed turn through the active provider (no-op for Null)."""
    provider = get_memory_registry().active()
    if provider is None or not provider.is_external:
        return  # Null/built-in: nothing to persist externally
    try:
        await provider.sync_turn(user_content, assistant_content,
                                 session_id=session_id, user_id=user_id)
    except Exception as e:
        logger.warning("memory_sync_turn failed: %s", e)


# ---- Episodic activity-ledger registry routers (Task 2) -------------------
# Mirror memory_sync_turn (writes) / kb_search (reads): no-throw, delegate to
# .active(), safe defaults on error. Tenant scoping stays the provider's job.

async def memory_record_episode(episode, *, session_id: str,
                                user_id: Optional[str] = None) -> None:
    """Route an episodic write through the active external provider (no-op for Null)."""
    provider = get_memory_registry().active()
    if provider is None or not provider.is_external:
        return
    try:
        await provider.record_episode(episode, session_id=session_id, user_id=user_id)
    except Exception as e:
        logger.warning("memory_record_episode failed: %s", e)


async def memory_recall_episodes(*, user_id: Optional[str] = None,
                                 since_ts: Optional[int] = None,
                                 until_ts: Optional[int] = None,
                                 kind: Optional[str] = None,
                                 thread_key: Optional[str] = None,
                                 limit: int = 20, order: str = "newest",
                                 exclude_surfaced: bool = False) -> list:
    """Route a time-ordered episodic recall through the active provider. [] on error."""
    provider = get_memory_registry().active()
    if provider is None:
        return []
    try:
        return await provider.recall_episodes(
            user_id=user_id, since_ts=since_ts, until_ts=until_ts, kind=kind,
            thread_key=thread_key, limit=limit, order=order,
            exclude_surfaced=exclude_surfaced)
    except Exception as e:
        logger.warning("memory_recall_episodes failed: %s", e)
        return []


# ---- KB (knowledge-base) registry routers (Task 5) -------------------------
# Mirror memory_search: no-throw, delegate to .active(), safe defaults on error.

async def kb_search(query: str, *, user_id: Optional[str] = None,
                    collection: str = "default", limit: int = 8) -> str:
    """Route a KB search through the active provider. '' on no-provider / error."""
    provider = get_memory_registry().active()
    if provider is None:
        return ""
    try:
        return await provider.kb_search(query, user_id=user_id,
                                        collection=collection, limit=limit)
    except Exception as e:
        logger.warning("kb_search failed: %s", e)
        return ""


async def kb_list_sources(*, user_id: Optional[str] = None,
                          collection: Optional[str] = None) -> list:
    """Route a KB source listing through the active provider. [] on error."""
    provider = get_memory_registry().active()
    if provider is None:
        return []
    try:
        return await provider.kb_list_sources(user_id=user_id, collection=collection)
    except Exception as e:
        logger.warning("kb_list_sources failed: %s", e)
        return []


async def kb_remove(*, user_id: Optional[str] = None, collection: str,
                    source: Optional[str] = None) -> int:
    """Route a KB removal through the active provider. 0 on error."""
    provider = get_memory_registry().active()
    if provider is None:
        return 0
    try:
        return await provider.kb_remove(user_id=user_id, collection=collection,
                                        source=source)
    except Exception as e:
        logger.warning("kb_remove failed: %s", e)
        return 0


async def kb_ingest_chunk(*, user_id: Optional[str] = None, collection: str,
                          source_path: str, source_hash: str, chunk_idx: int,
                          content: str, mime: str = "text/plain",
                          created_at: str = None) -> bool:
    """Route a KB chunk ingest through the active provider. Returns True on a
    successful write, False on no-provider / error / provider-refusal — so the
    ingest engine can detect a partial write instead of silently completing."""
    provider = get_memory_registry().active()
    if provider is None:
        return False
    try:
        return bool(await provider.kb_ingest_chunk(
            user_id=user_id, collection=collection, source_path=source_path,
            source_hash=source_hash, chunk_idx=chunk_idx, content=content,
            mime=mime, created_at=created_at,
        ))
    except Exception as e:
        logger.warning("kb_ingest_chunk failed: %s", e)
        return False


async def kb_source_hash(*, user_id: Optional[str] = None, collection: str,
                         source_path: str) -> Optional[str]:
    """Route a KB source-hash lookup through the active provider. None on error."""
    provider = get_memory_registry().active()
    if provider is None:
        return None
    try:
        return provider.kb_source_hash(user_id=user_id, collection=collection,
                                       source_path=source_path)
    except Exception as e:
        logger.warning("kb_source_hash failed: %s", e)
        return None
