"""Semantic retrieval for cross-phase memory access.

✅ ENABLED BY DEFAULT - Uses FREE local embeddings (no API costs).

This feature uses SentenceTransformer (Hugging Face local model) to find relevant
memories from any phase in the current session using vector similarity search.
All embedding calculations run locally on your server - NO API calls.

Enables the agent to access earlier findings even when they're not in the current
phase's context, which is especially useful for multi-phase research tasks.

Example:
    Agent in "processing" phase needs pricing data from "collection" phase.
    Semantic search finds: "DataCo pricing: Pro $99, Enterprise $199" (95% similarity)

Performance: Small CPU/memory overhead for embedding calculations, but no API costs.

Reference:
    Phase 3 Plan: docs/PHASE3_FINAL.md
"""

import logging
import numpy as np
from typing import List, Tuple, Dict, Any, Optional
from functools import lru_cache

logger = logging.getLogger(__name__)


class SemanticRetriever:
    """Semantic retrieval for finding relevant memories across phases.

    Uses vector embeddings to find semantically similar findings from any phase
    in the current session. This enables cross-phase knowledge access.

    Attributes:
        rag_manager: RAGKnowledgeManager instance for embeddings
        embedding_cache: LRU cache for embedding vectors
        min_similarity: Minimum cosine similarity threshold (0-1)
    """

    def __init__(
        self,
        rag_manager: Any,
        min_similarity: float = 0.65,
        cache_size: int = 100
    ):
        """Initialize semantic retriever.

        Args:
            rag_manager: RAGKnowledgeManager instance with embedding_model
            min_similarity: Minimum similarity threshold (default: 0.65)
            cache_size: Size of embedding cache (default: 100)
        """
        self.rag_manager = rag_manager
        self.min_similarity = min_similarity
        self.cache_size = cache_size
        self._embedding_cache: Dict[str, np.ndarray] = {}

        # Validate that we have an embedding model
        if not hasattr(rag_manager, 'embedding_model') or not rag_manager.embedding_model:
            raise ValueError("RAG manager must have a valid embedding_model")

        logger.info(f"✅ SemanticRetriever initialized (min_similarity={min_similarity})")

    def _get_cache_key(self, text: str) -> str:
        """Generate cache key for text.

        FIX #3: Use hash of full text to prevent cache collisions.
        Previously used first 100 chars which caused collisions for texts
        with similar prefixes.
        """
        import hashlib
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    def embed_text(self, text: str) -> Optional[np.ndarray]:
        """Embed text into vector representation with caching.

        Args:
            text: Text to embed

        Returns:
            Numpy array of embedding vector, or None if embedding fails
        """
        if not text or not text.strip():
            return None

        # Check cache
        cache_key = self._get_cache_key(text)
        if cache_key in self._embedding_cache:
            return self._embedding_cache[cache_key]

        try:
            # Get embedding from RAG manager's model.
            # show_progress_bar=False: SentenceTransformer.encode() otherwise prints
            # tqdm "Batches: 100%|...|" bars to stderr, which corrupt the CLI's Rich UI.
            try:
                embedding = self.rag_manager.embedding_model.encode(
                    text, show_progress_bar=False
                )
            except TypeError:
                # Embedding model doesn't accept show_progress_bar (non-SentenceTransformer)
                embedding = self.rag_manager.embedding_model.encode(text)

            # Convert to numpy array if needed
            if isinstance(embedding, list):
                embedding = np.array(embedding)

            # Cache it (limit cache size)
            if len(self._embedding_cache) >= self.cache_size:
                # Remove oldest entry (first item)
                first_key = next(iter(self._embedding_cache))
                del self._embedding_cache[first_key]

            self._embedding_cache[cache_key] = embedding

            return embedding

        except Exception as e:
            logger.error(f"❌ Failed to embed text: {e}")
            return None

    @staticmethod
    def cosine_similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
        """Calculate cosine similarity between two vectors.

        Args:
            vec1: First vector
            vec2: Second vector

        Returns:
            Similarity score between 0 and 1 (1 = identical)
        """
        # Handle zero vectors
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        # Cosine similarity
        similarity = np.dot(vec1, vec2) / (norm1 * norm2)

        # Clamp to [0, 1] range
        return float(max(0.0, min(1.0, similarity)))

    def search_similar(
        self,
        query: str,
        findings: Dict[str, List[str]],
        top_k: int = 3
    ) -> List[Tuple[str, str, float]]:
        """Search for semantically similar findings across all phases.

        Args:
            query: Query text (usually from brain_state)
            findings: Dict of {phase_name: [finding1, finding2, ...]}
            top_k: Number of top results to return (default: 3)

        Returns:
            List of (finding, phase, score) tuples, sorted by score descending
        """
        if not query or not findings:
            return []

        # Embed the query
        query_embedding = self.embed_text(query)
        if query_embedding is None:
            logger.warning("⚠️ Failed to embed query, skipping semantic search")
            return []

        # Search all findings
        results: List[Tuple[str, str, float]] = []

        for phase_name, phase_findings in findings.items():
            if not phase_findings:
                continue

            for finding in phase_findings:
                if not finding or not finding.strip():
                    continue

                # Embed the finding
                finding_embedding = self.embed_text(finding)
                if finding_embedding is None:
                    continue

                # Calculate similarity
                similarity = self.cosine_similarity(query_embedding, finding_embedding)

                # Filter by threshold
                if similarity >= self.min_similarity:
                    results.append((finding, phase_name, similarity))

        # Sort by similarity (descending) and take top_k
        results.sort(key=lambda x: x[2], reverse=True)

        if results:
            logger.debug(f"🔍 Found {len(results)} semantic matches for query: '{query[:50]}...'")

        return results[:top_k]

    def clear_cache(self) -> None:
        """Clear the embedding cache."""
        self._embedding_cache.clear()
        logger.debug("🗑️ Embedding cache cleared")

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dict with cache_size, cache_limit, hit_rate (if tracked)
        """
        return {
            'cache_size': len(self._embedding_cache),
            'cache_limit': self.cache_size,
            'min_similarity': self.min_similarity
        }
