"""The run records an explicit search reads beside ``memories``.

``memories`` holds what reflection chose to keep. A run's own record stays in ``episodes``, which
has no text index. An explicit search is rare, so it builds a bm25 index in memory for that one
call and adds no schema and no writer. It is never part of an automatic recall.

The owner thread is not read here. On the recall eval it added one answer and raised the rate of
stale state values from 0.17 to 0.30.
"""
import sqlite3
from typing import List

from core.sqlite_util import execute_retry
from modules.memory.fts_terms import select_terms, tokens
from modules.memory.recall_rank import rerank, strip_unverified

#: The newest rows one search reads.
RAW_RECORD_SCAN_ROWS = 10_000
#: The rows the index hands to ``rerank`` per requested row.
_POOL_PER_ROW = 4


def best_rows(query: str, docs: List[dict], limit: int) -> List[dict]:
    """The ``limit`` best of ``docs`` (``content``, ``ts``) for ``query``: bm25 x recency, and
    the newer of two near-duplicates. [] when ``query`` has no term or nothing matches."""
    terms = select_terms(tokens(query))
    if not terms or not docs:
        return []
    match = " OR ".join('"{}"'.format(t.replace('"', "")) for t in terms)
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE raw USING fts5(content)")
        conn.executemany("INSERT INTO raw (rowid, content) VALUES (?, ?)",
                         ((i, d["content"]) for i, d in enumerate(docs)))
        hits = conn.execute("SELECT rowid, rank FROM raw WHERE raw MATCH ? ORDER BY rank LIMIT ?",
                            (match, limit * _POOL_PER_ROW)).fetchall()
    finally:
        conn.close()
    return rerank([{**docs[i], "rank": rank} for i, rank in hits], limit=limit,
                  demote_automation=False)


def episode_docs(db_path: str, norm_user: str, *, drop_unverified: bool = False) -> List[dict]:
    """One tenant's run records as search documents, newest first. A read failure raises."""
    rows = execute_retry(
        db_path,
        "SELECT ts, kind, task, outcome, summary FROM episodes WHERE user_id = ? "
        "ORDER BY ts DESC LIMIT ?", (norm_user, RAW_RECORD_SCAN_ROWS), fetch="all") or []
    docs = []
    for r in rows:
        summary = r["summary"] or ""
        if drop_unverified:
            summary = strip_unverified(summary)
        head = " ".join(p for p in (r["kind"], r["outcome"]) if p)
        body = " ".join(p for p in (r["task"], summary) if p)
        if body:
            docs.append({"content": f"{head}: {body}", "ts": r["ts"]})
    return docs
