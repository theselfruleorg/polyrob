"""Pluggable memory provider interface (roadmap P7, Reference §27).

Establishes the seam for swapping memory backends behind one interface, with the
one-external-provider constraint that keeps tool-schema bloat in check. This is
the abstraction only — the live agent path is not rewired here; a follow-up
routes prefetch/sync_turn through ``MemoryProviderRegistry.active()``.

Interface shape (from Reference ``MemoryProvider(ABC)``):
- identity ......... ``name``, ``is_external``
- lifecycle ........ ``is_available``, ``initialize``, ``shutdown``
- stateless ........ ``get_tool_schemas``
- stateful ......... ``prefetch``, ``sync_turn``, ``search``
- optional hooks ... ``on_session_end``, ``on_pre_compress`` (default no-ops; wired)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class MemoryProviderError(RuntimeError):
    """Raised on provider-registration violations (e.g. >1 external provider)."""


@dataclass
class EpisodeRecord:
    """One durable activity/provenance row: what a completed run did, when.

    Distinct from the relevance `memories` store — this is time-ordered episodic
    provenance (answers "what did I do since X"), never full-text-searched.
    """
    ts: int                                   # unix epoch secs UTC — run COMPLETION time
    user_id: str
    session_id: str
    kind: str                                 # 'chat' | 'goal' | 'cron' | 'subagent'
    task: Optional[str] = None
    outcome: Optional[str] = None             # 'done'|'failed'|'partial'|'cancelled' (+status)
    summary: Optional[str] = None
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    spend_usd: float = 0.0
    steps: int = 0
    goal_id: Optional[str] = None
    thread_key: Optional[str] = None          # durable chat binding; None for goal/cron
    started_ts: Optional[int] = None
    meta: Optional[Dict[str, Any]] = None


class MemoryProvider(ABC):
    """Base class for memory backends."""

    # --- identity ------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    def is_external(self) -> bool:
        """External providers count against the one-provider limit. Built-in/Null
        providers return False so they can coexist with an external one."""
        return True

    # --- stateful (must implement) ------------------------------------------

    @abstractmethod
    async def prefetch(self, query: str, *, session_id: str, user_id: Optional[str] = None) -> str:
        """Return relevant context to inject before a turn (may be empty).

        ``user_id`` scopes recall to a single tenant (P0-0); ``None`` is the shared
        anonymous bucket for single-user/local use.
        """

    async def search(self, query: str, *, user_id: Optional[str] = None,
                     session_id: Optional[str] = None, limit: int = 5,
                     sort: Optional[str] = None) -> str:
        """Agent-callable recall (UP-09). Default implementation delegates to
        ``prefetch`` (ignoring limit/sort/browse) so providers that don't override it
        still work. Backends with a richer store (SQLite FTS) override this to support
        bounded result counts, sort, and browse-on-empty-query.
        """
        return await self.prefetch(query, session_id=session_id or "", user_id=user_id)

    @abstractmethod
    async def sync_turn(self, user_content: str, assistant_content: str, *,
                        session_id: str, user_id: Optional[str] = None) -> None:
        """Persist a completed turn into memory (scoped to ``user_id``)."""

    # --- lifecycle (default ok) ---------------------------------------------

    async def is_available(self) -> bool:
        return True

    async def initialize(self, *, session_id: str, **kwargs: Any) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    # --- stateless (default empty) ------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    # --- optional hooks (default no-op) -------------------------------------

    async def on_session_end(self, *, session_id: str, **kwargs: Any) -> None:
        return None

    async def on_pre_compress(self, *, session_id: str, **kwargs: Any) -> None:
        return None

    # --- KB (knowledge-base) storage — default no-ops (Task 5) -------------
    # Non-abstract: every provider satisfies the interface without implementing.
    # Concrete KB storage lives in SqliteMemoryProvider; NullMemoryProvider
    # inherits these no-ops automatically.

    async def kb_ingest_chunk(self, *, user_id, collection: str, source_path: str,
                              source_hash: str, chunk_idx: int, content: str,
                              mime: str = "text/plain", created_at: str = None) -> None:
        return None

    async def kb_search(self, query: str, *, user_id, collection: str = "default",
                        limit: int = 8) -> str:
        return ""

    async def kb_list_sources(self, *, user_id, collection: Optional[str] = None) -> List[Dict[str, Any]]:
        return []

    async def kb_remove(self, *, user_id, collection: str, source: Optional[str] = None) -> int:
        return 0

    def kb_source_hash(self, *, user_id, collection: str, source_path: str) -> Optional[str]:
        return None

    # --- episodic activity ledger — default no-ops (mirror KB methods) -------
    async def record_episode(self, episode: "EpisodeRecord", *,
                             session_id: str, user_id: Optional[str] = None) -> None:
        """Durably upsert one activity row keyed on session_id. Default: no-op."""
        return None

    async def recall_episodes(self, *, user_id: Optional[str] = None,
                              since_ts: Optional[int] = None, until_ts: Optional[int] = None,
                              kind: Optional[str] = None, thread_key: Optional[str] = None,
                              limit: int = 20, order: str = "newest",
                              exclude_surfaced: bool = False) -> List["EpisodeRecord"]:
        """Time-ordered activity recall. Default: no-op -> []."""
        return []

    def prune_episodes(self, *, older_than_ts: int) -> int:
        """Retention sweep hook (curator cadence). Default: no-op -> 0."""
        return 0

    def mark_episode_surfaced(self, *, session_id: str, user_id: Optional[str] = None) -> None:
        """Mark episode(s) as surfaced/delivered. Default: no-op."""
        return None


class NullMemoryProvider(MemoryProvider):
    """Inert provider — the default when no external memory backend is configured."""

    @property
    def name(self) -> str:
        return "null"

    @property
    def is_external(self) -> bool:
        return False

    async def prefetch(self, query: str, *, session_id: str, user_id: Optional[str] = None) -> str:
        return ""

    async def sync_turn(self, user_content: str, assistant_content: str, *,
                        session_id: str, user_id: Optional[str] = None) -> None:
        return None


class MemoryProviderRegistry:
    """Holds registered providers and enforces the one-external-provider limit.

    ``active()`` prefers the external provider when present, else the most recently
    registered built-in/Null provider.
    """

    def __init__(self) -> None:
        self._providers: List[MemoryProvider] = []

    def register(self, provider: MemoryProvider) -> MemoryProvider:
        if provider.is_external and any(p.is_external for p in self._providers):
            existing = next(p for p in self._providers if p.is_external)
            raise MemoryProviderError(
                f"only one external memory provider allowed; '{existing.name}' is already "
                f"registered (tried to add '{provider.name}')"
            )
        self._providers.append(provider)
        return provider

    def active(self) -> Optional[MemoryProvider]:
        for p in self._providers:
            if p.is_external:
                return p
        return self._providers[-1] if self._providers else None

    @property
    def providers(self) -> List[MemoryProvider]:
        return list(self._providers)

    def clear(self) -> None:
        self._providers.clear()
