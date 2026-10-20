"""Process-global memory-provider registry + agent-facing helpers (roadmap P7).

Makes the P7 ``MemoryProviderRegistry`` a live, consumable singleton and gives the
agent two no-throw entry points (``memory_prefetch`` / ``memory_sync_turn``) that
route through the *active* provider. The default active provider is
``NullMemoryProvider`` — so when no external backend is configured these are
no-ops and production behavior is unchanged. Registering an external provider
(``set_external_memory_provider``) makes the live agent start using it.
"""
from __future__ import annotations

import inspect
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


_warned_scopeless: set = set()


def _scope_kwargs(method, session_id) -> dict:
    """025: ``{"scope": spec}`` when the provider method accepts it.

    The spec comes from the session binding (``modules.memory.scope``), so every
    router caller — the per-step drain, the close drain, prefetch, session_search
    — carries its session's scope without a second plumbing path. A provider that
    does NOT accept ``scope`` while scopes are ON gets ONE warning naming it:
    isolation silently absent is never acceptable."""
    from modules.memory.scope import scopes_enabled, session_scope
    if not scopes_enabled():
        return {}
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        params = {}
    if "scope" in params:
        return {"scope": session_scope(session_id)}
    target = getattr(method, "__self__", None)
    if target is not None and not getattr(target, "is_external", True):
        return {}  # the Null/built-in provider stores nothing to isolate
    owner = type(target if target is not None else method).__name__
    if owner not in _warned_scopeless:
        _warned_scopeless.add(owner)
        logger.warning("memory scopes requested but provider %s does not support them "
                       "— isolation is OFF for it", owner)
    return {}


class MemoryReadError(RuntimeError):
    """The memory store could not be READ — distinct from "nothing matched".

    Raised by the recall routers only when the caller passes
    ``raise_errors=True`` (the agent-facing recall paths, which must tell the
    model "memory could not be read", never "no match"). ``error_class`` names
    the underlying exception type for that message."""

    def __init__(self, cause: BaseException):
        self.error_class = type(cause).__name__
        super().__init__(f"memory could not be read ({self.error_class})")


#: The reader description a recall router forwards when the provider declares it
#: (2026-10-13 review D2): the agents tier resolves the session regime and passes
#: plain values — ``modules`` never imports ``agents``.
_READER_KWARGS = ("demote_automation", "prefer_job_id", "prefer_goal_id")


def _reader_kwargs(method, reader: dict) -> dict:
    if not reader:
        return {}
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in reader.items()
            if k in _READER_KWARGS and k in params and v is not None}


async def memory_prefetch(query: str, *, session_id: str, user_id: Optional[str] = None,
                          raise_errors: bool = False, **reader) -> str:
    """Route a prefetch through the active provider; '' for Null / on any error.

    ``user_id`` scopes recall to the tenant (P0-0) so one user's memory never leaks
    into another's recall in a multi-tenant deployment. ``reader`` (any of
    ``demote_automation`` / ``prefer_job_id`` / ``prefer_goal_id``) is forwarded
    when the provider declares it. ``raise_errors=True`` turns a provider failure
    into ``MemoryReadError`` instead of '' (D3).
    """
    provider = get_memory_registry().active()
    if provider is None:
        return ""
    try:
        return await provider.prefetch(query, session_id=session_id, user_id=user_id,
                                       **_scope_kwargs(provider.prefetch, session_id),
                                       **_reader_kwargs(provider.prefetch, reader))
    except Exception as e:  # never break the agent loop on a memory backend hiccup
        logger.warning("memory_prefetch failed: %s", e)
        if raise_errors:
            raise MemoryReadError(e) from e
        return ""


async def memory_search(query: str, *, session_id: str = "", user_id: Optional[str] = None,
                        limit: int = 5, sort: Optional[str] = None,
                        before_id: Optional[int] = None, with_ids: bool = False,
                        raise_errors: bool = False, **reader) -> str:
    """Route an agent-initiated recall (discover/browse) through the active provider.

    Unlike ``memory_prefetch`` (automatic, fixed top_k, rank-only), this exposes the
    provider's richer ``search`` (bounded limit, sort, browse-on-empty). '' for Null
    / on any error. Tenant-scoped via ``user_id`` exactly like prefetch.

    T2.6: ``before_id``/``with_ids`` (rowid-cursor pagination + id display) are only
    forwarded when the active provider's ``search`` actually declares them — a
    provider/test-double that predates T2.6 (e.g. a bare 5-arg stub) still gets the
    exact legacy call it always got, rather than a TypeError. Real providers
    (SqliteMemoryProvider, LocalVectorMemoryProvider) declare both.
    """
    provider = get_memory_registry().active()
    if provider is None:
        return ""
    kwargs = {}
    try:
        params = inspect.signature(provider.search).parameters
    except (TypeError, ValueError):
        params = {}
    if before_id is not None and "before_id" in params:
        kwargs["before_id"] = before_id
    if with_ids and "with_ids" in params:
        kwargs["with_ids"] = with_ids
    kwargs.update(_scope_kwargs(provider.search, session_id))
    kwargs.update(_reader_kwargs(provider.search, reader))
    try:
        return await provider.search(query, user_id=user_id, session_id=session_id,
                                     limit=limit, sort=sort, **kwargs)
    except Exception as e:
        logger.warning("memory_search failed: %s", e)
        if raise_errors:
            raise MemoryReadError(e) from e
        return ""


#: ``memory_sync_turn`` outcomes (D4, review 2026-10-13). Only ``INSERTED`` is a
#: new row; the ``memory_write`` event is recorded for that one alone.
SYNC_INSERTED = "inserted"
SYNC_DUPLICATE = "duplicate"     # exact duplicate: collapsed into a ts refresh
SYNC_SKIPPED = "skipped"         # nothing to store (filtered / anon / Null provider)
SYNC_REFUSED_CAP = "refused_cap" # the tenant storage ceiling refused the row
SYNC_FAILED = "failed"

#: The owner-visible standing verdict for a full store (``core.credential_verdicts``
#: renders it as a /status health line). Recorded at most once a day per process.
STORE_FULL_KIND = "memory_store"
STORE_FULL_KEY = "cross_session"
_STORE_FULL_EVERY_SEC = 86400.0
_store_full = {"recorded_at": 0.0, "open": None}


def _note_store_full() -> None:
    import time as _t
    now = _t.time()
    # Rate-limit only while the verdict is known OPEN: after a real insert
    # cleared it, the store filling up again must be told at once — not up
    # to a day later.
    if (_store_full["open"] is not False
            and now - _store_full["recorded_at"] < _STORE_FULL_EVERY_SEC):
        return
    _store_full["recorded_at"] = now
    _store_full["open"] = True
    try:
        from core.credential_verdicts import record_rejection
        from modules.memory.storage_limits import MAX_BYTES, MAX_MEMORY_ROWS
        record_rejection(
            STORE_FULL_KIND, STORE_FULL_KEY, code="storage limit reached",
            remedy=(f"cross-session memory is full ({MAX_MEMORY_ROWS:,} rows / "
                    f"{MAX_BYTES // (1024 * 1024)} MiB per tenant) — new findings are NOT "
                    "being stored; remove old memory rows to make room"),
            ttl_sec=_STORE_FULL_EVERY_SEC)
    except Exception as e:  # the alarm must never break the write path
        logger.debug("memory store-full verdict skipped: %s", e)


def _note_store_ok() -> None:
    """A real insert proves the store has room: close a standing full verdict.
    Reads the verdict store at most once per process while none is known open."""
    if _store_full["open"] is False:
        return
    try:
        from core.credential_verdicts import clear_rejection, verdict
        if _store_full["open"] or verdict(STORE_FULL_KIND, STORE_FULL_KEY) is not None:
            clear_rejection(STORE_FULL_KIND, STORE_FULL_KEY)
        _store_full["open"] = False
    except Exception as e:
        logger.debug("memory store-full verdict clear skipped: %s", e)


async def memory_sync_turn(user_content: str, assistant_content: str, *,
                           session_id: str, user_id: Optional[str] = None) -> str:
    """Route a completed turn through the active provider (no-op for Null).

    Returns one of the ``SYNC_*`` outcomes (D4): a refused or failed write used
    to be indistinguishable from a stored one, so the ``memory_write`` event
    claimed writes the store had refused. Never raises."""
    provider = get_memory_registry().active()
    if provider is None or not provider.is_external:
        return SYNC_SKIPPED  # Null/built-in: nothing to persist externally
    from modules.memory.storage_limits import StorageLimitError
    try:
        got = await provider.sync_turn(user_content, assistant_content,
                                       session_id=session_id, user_id=user_id,
                                       **_scope_kwargs(provider.sync_turn, session_id))
    except StorageLimitError as e:
        logger.warning("memory_sync_turn refused: %s", e)
        _note_store_full()
        return SYNC_REFUSED_CAP
    except Exception as e:
        logger.warning("memory_sync_turn failed: %s", e)
        return SYNC_FAILED
    if got is True:
        _note_store_ok()
        return SYNC_INSERTED
    return SYNC_DUPLICATE if got is False else SYNC_SKIPPED


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
                                 exclude_surfaced: bool = False,
                                 raise_errors: bool = False) -> list:
    """Route a time-ordered episodic recall through the active provider. [] on error,
    or ``MemoryReadError`` when ``raise_errors=True`` (a run's own previous-runs
    read must not mistake an unreadable store for a first run)."""
    provider = get_memory_registry().active()
    if provider is None:
        return []
    extra = {}
    if raise_errors:
        try:
            if "raise_errors" in inspect.signature(provider.recall_episodes).parameters:
                extra["raise_errors"] = True
        except (TypeError, ValueError):
            pass
    try:
        return await provider.recall_episodes(
            user_id=user_id, since_ts=since_ts, until_ts=until_ts, kind=kind,
            thread_key=thread_key, limit=limit, order=order,
            exclude_surfaced=exclude_surfaced, **extra)
    except Exception as e:
        logger.warning("memory_recall_episodes failed: %s", e)
        if raise_errors:
            raise MemoryReadError(e) from e
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


async def kb_replace_source(*, user_id: Optional[str] = None, collection: str,
                            source_path: str, source_hash: str, chunks: list[str],
                            mime: str = "text/plain", created_at: str = None) -> bool:
    """Publish one complete source; refuse absent/nontransactional providers."""
    provider = get_memory_registry().active()
    if provider is None:
        return False
    try:
        return bool(await provider.kb_replace_source(
            user_id=user_id, collection=collection, source_path=source_path,
            source_hash=source_hash, chunks=chunks, mime=mime, created_at=created_at,
        ))
    except Exception as e:
        logger.warning("kb_replace_source failed: %s", e)
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
