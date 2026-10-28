"""Recall ranking: who reads, how old, and which of two near-twins wins.

2026-10-13 memory/coherency review (D2, A3, A7): recall ordered rows by a
fixed "chat before cron/goal" key and then by FTS5 bm25 rank alone. So

- a cron or goal run never saw its OWN previous runs (they are cron/goal rows,
  sorted after every chat row that shared one word), and
- a 10-05 "the swaps stopped" row beat the 10-11 "announce the swaps on X"
  row: nothing in the order knew which one was newer.

This module is the ONE place that turns the provider's candidate pool into the
returned order. Pure functions, stdlib only; the caller (the sqlite provider)
supplies, per row, its bm25 ``rank``, its write ``ts`` and two flags computed
in SQL: ``own`` (the row belongs to a past run of the reader's own cron job or
goal) and ``auto`` (the row belongs to some cron/goal run).

The reader is described by plain parameters (``modules`` may not import
``agents``): the agents tier resolves the session regime and passes
``demote_automation`` and the job / goal id it prefers.
"""
import math
import re
import time
from typing import Iterable, List, Optional

#: Days for the recency weight to fall halfway to its floor.
RECENCY_HALF_LIFE_DAYS = 14.0
#: The weight never falls below this — an old owner ruling still ranks.
RECENCY_FLOOR = 0.3
#: Age assumed for a legacy row with no write stamp.
UNDATED_AGE_DAYS = 365.0
#: Word-set overlap at or above which two rows say the same thing. Numbers
#: count as words, so "widget 1" and "widget 2" stay two rows; a restated
#: finding with one changed word is one.
NEAR_DUP_JACCARD = 0.8
#: Rows shorter than this many distinct words are never merged.
NEAR_DUP_MIN_WORDS = 4

_WORD = re.compile(r"\w+", re.UNICODE)


def recency_weight(ts: Optional[int], *, now: Optional[float] = None) -> float:
    """1.0 for a row written now, falling to ``RECENCY_FLOOR`` with age."""
    now = time.time() if now is None else now
    if ts:
        age_days = max(0.0, (now - float(ts)) / 86400.0)
    else:
        age_days = UNDATED_AGE_DAYS
    return RECENCY_FLOOR + (1.0 - RECENCY_FLOOR) * math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)


def _words(text: str) -> frozenset:
    return frozenset(w.lower() for w in _WORD.findall(text or ""))


def near_duplicate(a: str, b: str) -> bool:
    """True when two rows share most of their words (Jaccard on word sets)."""
    wa, wb = _words(a), _words(b)
    if len(wa) < NEAR_DUP_MIN_WORDS or len(wb) < NEAR_DUP_MIN_WORDS:
        return False
    return len(wa & wb) / len(wa | wb) >= NEAR_DUP_JACCARD


def rerank(rows: Iterable[dict], *, limit: int, demote_automation: bool = True,
           now: Optional[float] = None) -> List[dict]:
    """Order a candidate pool and cut it to ``limit``.

    Each row: ``content``, ``ts`` (may be None), ``rank`` (FTS5 bm25, lower =
    better; may be None), ``own`` and ``auto`` (truthy flags).

    1. Rows of the reader's own job / goal first.
    2. When ``demote_automation`` (an owner-chat reader), cron/goal rows after
       chat rows — the old order, now a per-reader choice.
    3. Inside a group: relevance x recency.
    4. Supersession: of two near-duplicate rows only the NEWER one is kept,
       at the better position of the two.
    """
    pool = list(rows)

    def score(r: dict) -> float:
        rel = -float(r["rank"]) if r.get("rank") is not None else 0.0
        rel = max(rel, 1e-6)
        return rel * recency_weight(r.get("ts"), now=now)

    def key(r: dict):
        return (0 if r.get("own") else 1,
                (1 if r.get("auto") else 0) if demote_automation else 0,
                -score(r))

    kept: List[dict] = []
    for r in sorted(pool, key=key):
        twin = next((i for i, k in enumerate(kept)
                     if near_duplicate(k["content"], r["content"])), None)
        if twin is None:
            kept.append(r)
        elif (r.get("ts") or 0) > (kept[twin].get("ts") or 0):
            kept[twin] = r          # the newer statement supersedes the older
    return kept[:limit]
