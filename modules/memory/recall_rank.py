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

#: F7 (prod review 2026-10-29): an owner-chat recall reads only EPISODIC rows
#: from the last N days, unless the owner's message names an older date. 96 %
#: of prod rows were older than two weeks, and a "did we pay?" question got
#: 10-14 rows. A durable fact (an address, a handle, a contract id, a
#: preference) has no age limit (review M5).
OWNER_RECALL_MAX_AGE_DAYS = 7
#: The marker of an episodic row: an account of what one run did (the phase
#: summaries ``memory_writer`` / the close drain write, ``task_context_manager``).
#: Only such a row ages out of an owner-chat recall; any other row is a fact.
EPISODIC_MARK = "[phase-summary:"
#: A row whose content starts with this marker is a claim the run could not
#: check (``memory_writer`` / ``reflection_service``). An owner chat never
#: gets it as recall.
UNVERIFIED_MARK = "[unverified]"

_WORD = re.compile(r"\w+", re.UNICODE)
_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december")
#: A month word is the full name or its 3-letter (or "sept") abbreviation —
#: never a prefix ("marketing", "decks" and "junk" are not months).
_MONTHS = {**{n: i for i, n in enumerate(_MONTH_NAMES, 1)},
           **{n[:3]: i for i, n in enumerate(_MONTH_NAMES, 1)}, "sept": 9}
_ISO_DATE = re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b")
#: A bare ``10-14`` / ``10/14`` is a date only after a day word ("on 10-14",
#: "since 10/14"): "send 1/2 of the balance" and "post 3-4 tweets" are not.
_MM_DD = re.compile(r"\b(?:on|since|from|before|after|until|till|by)\s+"
                    r"(\d{1,2})[-/](\d{1,2})(?![\w./-])", re.IGNORECASE)
_MON_DAY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b")
_DAY_MON = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]{3,9})\b")


def is_episodic(content: str) -> bool:
    """True for a row that is an account of one run (it ages out of an
    owner-chat recall); False for a durable fact."""
    return EPISODIC_MARK in (content or "")


def _named_dates(text: str, year: int):
    """(month, day, year) triples a free-text message names. Pure, best-effort."""
    for y, m, d in _ISO_DATE.findall(text):
        yield int(m), int(d), int(y)
    for m, d in _MM_DD.findall(text):
        yield int(m), int(d), year
    for mon, d in _MON_DAY.findall(text):
        if mon.lower() in _MONTHS:
            yield _MONTHS[mon.lower()], int(d), year
    for d, mon in _DAY_MON.findall(text):
        if mon.lower() in _MONTHS:
            yield _MONTHS[mon.lower()], int(d), year


def strip_unverified(content: str) -> str:
    """``content`` without its ``[unverified]`` lines ("" when none is left)."""
    lines = [ln for ln in (content or "").split("\n")
             if not ln.lstrip().startswith(UNVERIFIED_MARK)]
    return "\n".join(lines).strip()


def names_older_date(query: str, *, max_age_days: float,
                     now: Optional[float] = None) -> bool:
    """True when ``query`` names a calendar date older than ``max_age_days``
    ("2026-10-14", "on 10-14", "Oct 14", "14 October"). A date with no year is
    read in the current year. The owner asked about that day, so recall must
    not cut it away."""
    now = time.time() if now is None else now
    year = time.localtime(now).tm_year
    cutoff = now - float(max_age_days) * 86400.0
    import datetime as _dt
    for m, d, y in _named_dates(query or "", year):
        try:
            _dt.date(y, m, d)          # a real calendar day (no 2-30, no 13-01)
            ts = time.mktime((y, m, d, 23, 59, 59, 0, 0, -1))
        except (OverflowError, ValueError):
            continue
        if ts < cutoff:
            return True
    return False


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
