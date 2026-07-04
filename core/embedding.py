"""Lazy sentence-transformers embedder proxy.

Constructing a SentenceTransformer is expensive — it imports torch (~6s cold) and, worse,
validates the cached model against the HuggingFace Hub over the network on every launch
(~8s of HTTP HEAD round-trips). Doing that synchronously while building the CLI container
(or the server lifespan) blocks the prompt / first-ready for ~14s even when the active
memory backend (default sqlite/FTS5) never uses vectors.

LazyEmbedder defers the build to first actual use and loads from the local cache
(local_files_only=True, falling back to a one-time online download if uncached). It is a
transparent stand-in for a SentenceTransformer: callers keep doing
`container.get_service("embedding_model").encode(...)`. If nothing ever needs vectors, the
model is never built. See docs/plans/2026-06-26-runtime-architecture-finalization-FUSION.md.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _default_builder(model_name: str):
    """Build a SentenceTransformer from the local cache (no network) when possible."""
    from sentence_transformers import SentenceTransformer
    try:
        return SentenceTransformer(model_name, local_files_only=True)
    except Exception:
        # Not cached yet (first ever run) — allow the one-time online download.
        logger.info("embedder cache miss; downloading %s once", model_name)
        return SentenceTransformer(model_name)


class LazyEmbedder:
    """Transparent proxy that builds the underlying embedding model on first use."""

    def __init__(self, model_name: str, builder: Optional[Callable[[str], Any]] = None) -> None:
        self._model_name = model_name
        self._builder = builder or _default_builder
        self._model: Any = None
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    def _ensure(self) -> Any:
        if not self._loaded:
            self._model = self._builder(self._model_name)
            self._loaded = True
        return self._model

    def __getattr__(self, item: str) -> Any:
        # __getattr__ only fires for attributes not resolved normally (i.e. not the
        # private state set in __init__), so this delegates real model methods like encode().
        return getattr(self._ensure(), item)
