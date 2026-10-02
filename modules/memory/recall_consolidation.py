"""Consolidate repeated recall rows into ONE curated note (WS-K4, 2026-09-22).

Measured on prod 2026-09-22: **19,001 recall rows, 6.48 MB, and 0 curated
notes.** No exact duplicates — and plenty of near ones, because the same lesson
is learned again every few sessions and written down again in slightly
different words:

    x_browser_x_post caps at 280 chars; the API rail 402s and tw…
    x_browser_x_post rejects text over 280 chars; drafting a con…

Two rows, one fact. Nothing in the tree promoted a repeated lesson into
anything, so the distilled store designed to hold it stayed empty while the raw
store grew without bound. Learning wrote; it never consolidated.

**Deterministic, no LLM.** The CURATOR_LLM_MERGE lesson (a scaffolded merge
with no concrete policy that logged a no-op for months and was deleted) applies
exactly here: a clustering the owner cannot predict is worse than none. The
policy is stated, cheap and testable:

- A row's SIGNATURE is its rarest content tokens — the ones that carry the
  subject (`x_browser_x_post`, `280`) rather than the phrasing (`caps`,
  `rejects`, `over`). Rarity is measured against this tenant's own corpus, so
  a token that appears everywhere can never be a subject.
- A cluster of ``min_size`` rows or more becomes ONE note, titled with the
  count, bodied with the SHORTEST member (the shortest statement of a fact is
  usually the cleanest one), and sourced with the signature so a later tick
  updates that note instead of writing a second one.

⚠️ **Nothing is deleted.** Consolidation writes a note; the raw rows stay and
age out under ``MEMORY_RETENTION_DAYS`` as before. A consolidation that also
pruned would make one heuristic responsible for losing data.
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: How many rows must say the same thing before it is worth distilling.
MIN_CLUSTER = 3

#: A token appearing in more than this share of the corpus is phrasing, not a
#: subject, and can never anchor a signature.
MAX_DOC_FREQUENCY = 0.05

#: Tokens per signature — EXACTLY this many, never fewer and never more.
#: ⚠️ A variable-length signature is unstable under growth: a fourth row that
#: happens to carry one more qualifying token gets a longer signature, lands in
#: its own bucket, and the cluster it belongs to silently splits. Two is the
#: smallest number that does not group by coincidence.
SIGNATURE_TOKENS = 2

#: The rarity ceiling never drops below this, whatever the corpus size. At 5%
#: of a 60-row corpus the ceiling would be 3 — the same number as the minimum
#: cluster size — so a cluster's own subject would cross the ceiling the moment
#: the cluster grew by one and dissolve itself.
MIN_CEILING = 10

#: Notes one tick may write. A first run over a 19,001-row corpus must not
#: fill the per-tenant note store in a single pass.
MAX_NOTES_PER_RUN = 20

#: ⚠️ The CEILING on derived notes per tenant, and the reason it exists: every
#: other writer of this table goes through ``note_create``, which refuses past
#: ``MEMORY_TOOL_MAX_ENTRIES`` (50). This pass inserts directly — so without its
#: own ceiling a 19,001-row corpus could distil into hundreds of rows in a store
#: the rest of the system assumes is small, and `memory(read)` would hand the
#: agent all of them. Existing notes are always UPDATED; only new ones stop.
#: Clusters are served largest-first, so the ceiling keeps the most-repeated
#: lessons and drops the long tail.
MAX_CONSOLIDATION_NOTES = 50

#: The marker that makes a consolidation note recognisable and idempotent.
SOURCE_PREFIX = "consolidation:"

_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")
_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-f]{8,}\b")

#: Ordinary English glue plus this tree's own ambient vocabulary. Short words
#: are already excluded by the token pattern.
_STOPWORDS = frozenset("""
the and for with that this from into was were has have had not but its
you your they them then than when where which while who whom what why how
are can could should would will shall may might must about after again all
any because been before being both each few more most other some such only
own same too very just now off out over under once here there also did does
doing done get got make made take taken use used using one two three
agent session run step task tool call result error text file line code
""".split())


@dataclass
class Cluster:
    signature: str
    rows: List[Tuple[int, str]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.rows)

    @property
    def representative(self) -> str:
        """The shortest member — the cleanest statement of the shared fact."""
        return min((text for _, text in self.rows), key=len) if self.rows else ""


def tokens(text: str) -> List[str]:
    """Content tokens: lowercase, no hex blobs, no glue, no duplicates."""
    lowered = _HEX_RE.sub(" ", (text or "").lower())
    seen, out = set(), []
    for tok in _TOKEN_RE.findall(lowered):
        if tok in _STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def document_frequency(rows: Sequence[Tuple[int, str]]) -> Counter:
    df: Counter = Counter()
    for _, text in rows:
        df.update(tokens(text))
    return df


def signature(text: str, df: Counter, total: int,
              *, max_df: float = MAX_DOC_FREQUENCY,
              size: int = SIGNATURE_TOKENS,
              min_df: int = MIN_CLUSTER) -> Optional[str]:
    """The SUBJECT tokens of *text*, or None when it has none.

    ⚠️ A subject is a token that is rare in the corpus AND repeated. Both
    halves matter, and the first draft of this function had only the first:
    ranking by rarity alone picked ``402s``, ``caps``, ``api`` — the tokens
    that appear exactly once, which are precisely the PHRASING that differs
    between two statements of the same fact. The band is therefore
    ``min_df <= df <= ceiling``, ranked most-repeated first.

    Returning None is the honest answer for a row with nothing distinctive in
    it: such a row joins no cluster rather than joining the wrong one.
    """
    if total <= 0:
        return None
    ceiling = max(MIN_CEILING, int(total * max_df))
    subjects = [t for t in tokens(text)
                if int(min_df) <= df.get(t, 0) <= ceiling]
    if len(subjects) < size:
        return None
    subjects.sort(key=lambda t: (-df.get(t, 0), t))
    return " ".join(sorted(subjects[:size]))


def cluster_rows(rows: Sequence[Tuple[int, str]], *,
                 min_size: int = MIN_CLUSTER,
                 max_df: float = MAX_DOC_FREQUENCY) -> List[Cluster]:
    """Group rows that say the same thing. Largest cluster first."""
    total = len(rows)
    if total < min_size:
        return []
    df = document_frequency(rows)
    buckets: Dict[str, Cluster] = {}
    for rowid, text in rows:
        sig = signature(text, df, total, max_df=max_df, min_df=min_size)
        if not sig:
            continue
        buckets.setdefault(sig, Cluster(signature=sig)).rows.append((rowid, text))
    out = [c for c in buckets.values() if c.size >= min_size]
    out.sort(key=lambda c: (-c.size, c.signature))
    return out


def note_body(cluster: Cluster) -> str:
    return cluster.representative


def note_title(cluster: Cluster) -> str:
    return f"learned {cluster.size}×: {cluster.signature}"


class RecallConsolidationMixin:
    """Provider half: read the tenant's recall, write one note per cluster.

    Synchronous on purpose — it runs on the curator tick beside
    ``prune_memories`` and ``consolidate_notes``, which are sync for the same
    reason (the curator body is off the event loop).
    """

    def consolidate_recall(self, *, user_id: str, min_size: int = MIN_CLUSTER,
                           max_notes: int = MAX_NOTES_PER_RUN) -> Dict[str, int]:
        """Distil repeated recall into curated notes. Returns counts."""
        from core.sqlite_util import execute_retry

        out = {"clusters": 0, "written": 0, "updated": 0, "rows": 0,
               "at_ceiling": 0}
        if self._anon_blocked(user_id):
            return out
        norm = self._norm_user(user_id)
        rows = execute_retry(
            self.db_path,
            "SELECT rowid AS rid, content FROM memories WHERE user_id = ?",
            (norm,), fetch="all") or []
        corpus = [(int(dict(r)["rid"]), str(dict(r)["content"] or "")) for r in rows]
        out["rows"] = len(corpus)
        clusters = cluster_rows(corpus, min_size=min_size)
        out["clusters"] = len(clusters)
        if not clusters:
            return out

        # Every prior consolidation note, whatever its status. ⚠️ An ARCHIVED one
        # still counts: re-inserting beside it would put two rows for one cluster
        # in the store, and resurrecting it would overturn an owner who archived
        # it on purpose. It is updated in place and left archived.
        existing = {
            str(dict(r)["source"] or ""): dict(r)
            for r in (execute_retry(
                self.db_path,
                "SELECT id, source, content, status FROM curated_memory "
                "WHERE user_id = ? AND source LIKE ?",
                (norm, SOURCE_PREFIX + "%"), fetch="all") or [])
        }
        _, max_chars = self._curated_caps()
        now = int(time.time())
        live = sum(1 for r in existing.values()
                   if str(r.get("status") or "active") != "archived")
        for cluster in clusters:
            if out["written"] >= max(0, int(max_notes)):
                break
            if live >= MAX_CONSOLIDATION_NOTES and \
                    (SOURCE_PREFIX + cluster.signature) not in existing:
                out["at_ceiling"] = 1
                break
            source = SOURCE_PREFIX + cluster.signature
            body = note_body(cluster)[:max_chars]
            title = note_title(cluster)
            prior = existing.get(source)
            try:
                if prior is not None:
                    if str(prior.get("content") or "") == body:
                        continue
                    execute_retry(
                        self.db_path,
                        "UPDATE curated_memory SET content = ?, title = ?, "
                        "updated_ts = ? WHERE id = ? AND user_id = ?",
                        (body, title, now, prior["id"], norm))
                    out["updated"] += 1
                    continue
                execute_retry(
                    self.db_path,
                    "INSERT INTO curated_memory (user_id, content, title, tags, "
                    "links, source, created_ts, updated_ts, access_count, status, "
                    "created_by) VALUES (?,?,?,'[]','[]',?,?,?,0,'active','curator')",
                    (norm, body, title, source, now, now))
                out["written"] += 1
                live += 1
            except Exception as exc:
                logger.warning("recall consolidation write failed (%s): %s",
                               source, exc)
        return out


def tenants_with_recall(db_path: str) -> Iterable[str]:
    """Every tenant holding recall rows — the sweep is global, like its siblings.

    An absent path yields nothing rather than opening sqlite on it: connecting
    CREATES the file, and a maintenance pass must never bring a store into
    existence to find it empty.
    """
    import os

    from core.sqlite_util import execute_retry

    if not db_path or not os.path.exists(db_path):
        return
    rows = execute_retry(db_path, "SELECT DISTINCT user_id FROM memories",
                         fetch="all") or []
    for row in rows:
        uid = str(dict(row).get("user_id") or "")
        if uid:
            yield uid


__all__ = ["Cluster", "MAX_CONSOLIDATION_NOTES", "MAX_DOC_FREQUENCY",
           "MAX_NOTES_PER_RUN", "MIN_CLUSTER",
           "MIN_CEILING",
           "RecallConsolidationMixin", "SIGNATURE_TOKENS", "SOURCE_PREFIX",
           "cluster_rows", "document_frequency", "note_body", "note_title",
           "signature", "tenants_with_recall", "tokens"]
