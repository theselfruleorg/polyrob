"""Lexical (no-embedder) cross-phase finding retriever.

Drop-in for SemanticRetriever.search_similar so H-MEM section-3 (cross-phase recall)
works on the server (no torch) instead of silently degrading. Cosine over term-frequency
vectors — deterministic, dependency-free.
"""
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple


def _toks(s: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9$]+", (s or "").lower()) if len(t) > 1]


class LexicalRetriever:
    def __init__(self, min_similarity: float = 0.1):
        self.min_similarity = min_similarity

    def search_similar(self, query: str, findings: Dict[str, List[str]],
                       top_k: int = 3) -> List[Tuple[str, str, float]]:
        """Search for lexically similar findings across all phases.

        Args:
            query: Query text (usually from brain_state).
            findings: Dict of {phase_name: [finding1, finding2, ...]}.
            top_k: Number of top results to return.

        Returns:
            List of (finding, phase_name, score) tuples sorted by score descending.
        """
        if not query or not findings:
            return []
        q = Counter(_toks(query))
        qnorm = sum(v * v for v in q.values()) ** 0.5
        if qnorm == 0:
            return []
        results: List[Tuple[str, str, float]] = []
        for phase_name, phase_findings in findings.items():
            for finding in (phase_findings or []):
                if not finding or not finding.strip():
                    continue
                fc = Counter(_toks(finding))
                fnorm = sum(v * v for v in fc.values()) ** 0.5
                if fnorm == 0:
                    continue
                dot = sum(q[t] * fc[t] for t in q if t in fc)
                sim = dot / (qnorm * fnorm)
                if sim >= self.min_similarity:
                    results.append((finding, phase_name, sim))
        results.sort(key=lambda x: x[2], reverse=True)
        return results[:top_k]

    # ── parity stubs so the consumer can call these without guard branches ───

    def embed_text(self, text: str) -> Optional[Any]:  # noqa: ARG002
        """Parity stub — lexical path does not embed; returns None.

        Returning None causes ``hierarchical_search`` in ContextRetriever to
        bail out early (it checks ``if query_embedding is None``), which is the
        correct fallback behaviour when no embedder is available.
        """
        return None

    def clear_cache(self) -> None:
        """Parity no-op — no embedding cache to clear."""
        return None

    def get_cache_stats(self) -> Dict[str, Any]:
        """Parity no-op stats — no embedding cache."""
        return {"cache_size": 0, "cache_limit": 0, "min_similarity": self.min_similarity}
