"""Select + register the external memory backend from MEMORY_BACKEND.

Default: **sqlite** (cross-session FTS recall is ON by default; P0-1). Set
MEMORY_BACKEND=none/off/'' to fall back to NullMemoryProvider. Recall is tenant-scoped
(P0-0) and empty-user_id I/O is refused by default (MEMORY_REQUIRE_USER_ID, UP-03), so
default-on is multi-tenant-safe. Call once during agent/container construction."""
import logging
import os
from typing import Optional

from modules.memory.provider import MemoryProvider
from modules.memory.registry import get_memory_registry

logger = logging.getLogger(__name__)


def maybe_register_memory_backend(*, data_dir: Optional[str] = None,
                                  embedding_model=None) -> Optional[MemoryProvider]:
    """Register the configured external memory provider; return it (or None).

    Idempotent: safe to call once per agent construction. If an external provider is
    already active (e.g. a sibling agent in the same process registered it), return it
    instead of re-registering (which would raise the one-external-provider error).

    MEMORY_BACKEND:
      - ``sqlite``        (default) — FTS5 keyword recall (SqliteMemoryProvider).
      - ``local_vector``  — local hybrid keyword+vector recall (sqlite-vec); needs an
        ``embedding_model`` (reuses the container's local sentence-transformers model).
        Degrades to FTS5-only if the model/extension is unavailable (fail-open).
      - ``none``/``off``/``''`` — NullMemoryProvider (disabled).
    """
    # Default-on (P0-1): cross-session memory is enabled unless explicitly disabled.
    # Safe since recall is tenant-scoped (P0-0). Set MEMORY_BACKEND=none/off/'' to disable.
    # Phase E: under POLYROB_LOCAL the default is local_vector (semantic recall over the
    # answer-only facts from Phase 1.1) — but ONLY the default moves; an explicit
    # MEMORY_BACKEND always wins, and an empty value still means "off". If apsw/sqlite-vec
    # or the embedder is unavailable the provider degrades to FTS5 keyword recall
    # (fail-safe + loud-degrade warning), so the multi-tenant server stays on sqlite.
    from core.env import bool_env
    default_backend = "local_vector" if bool_env("POLYROB_LOCAL", False) else "sqlite"
    backend = os.getenv("MEMORY_BACKEND", default_backend).strip().lower()
    if backend not in ("sqlite", "local_vector"):
        return None
    registry = get_memory_registry()
    existing = registry.active()
    if existing is not None and getattr(existing, "is_external", False):
        # Already have an external provider this process — don't re-register.
        return existing
    # WS-3: an omitted data_dir resolves to the data home, never a relative "data".
    from core.runtime_paths import data_dir_or_home
    db_path = os.path.join(data_dir_or_home(data_dir), "memory.db")
    if backend == "local_vector":
        from modules.memory.local_vector_memory_provider import LocalVectorMemoryProvider, _vec_available
        # Graceful fallback: if sqlite-vec unavailable, fall back to FTS5
        if not _vec_available():
            logger.warning(
                "sqlite-vec extension unavailable (apsw or sqlite_vec not importable). "
                "Falling back to FTS5 keyword recall. To enable vector recall, "
                "install apsw and sqlite-vec."
            )
            from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
            provider = SqliteMemoryProvider(db_path)
        else:
            provider: MemoryProvider = LocalVectorMemoryProvider(
                db_path, embedding_model=embedding_model)
            # Phase 1.2: loud-degrade. The provider degrades to FTS5-only internally
            # when the embedder is missing or its probe fails — without this warning it
            # would register as healthy while serving keyword-only recall (the silent
            # "reports healthy while degraded" trap).
            if not getattr(provider, "_vec_ok", False):
                logger.warning(
                    "MEMORY_BACKEND=local_vector requested but vector recall is DISABLED "
                    "(no/failed embedding model). Serving FTS5 keyword recall only. Ensure "
                    "the sentence-transformers embedder is registered (embedder_needed)."
                )
    else:
        from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
        provider = SqliteMemoryProvider(db_path)
    try:
        registry.register(provider)
        logger.info("Registered external memory backend: %s", provider.name)
        return provider
    except Exception as e:  # raced with another registration; fall back to whatever is active
        logger.debug("memory backend already registered, reusing active: %s", e)
        return registry.active()
