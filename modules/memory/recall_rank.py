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

#: F7 (prod review 2026-10-29): an owner-chat recall reads only rows from the
#: last N days, unless the owner's message names an older date. 96 % of prod rows
#: were older than two weeks, and a "did we pay?" question got 10-14 rows.
OWNER_RECALL_MAX_AGE_DAYS = 7
#: An automatic recall injects no match with a bm25 score under this: one common word
#: from the query is no reason to put a row in front of the model. Nothing is the
#: allowed answer. Chosen on the recall eval (ops/lab/recall_eval.py).
RECALL_WEAK_SCORE = 2.0
#: bm25 weighs a term by the rows it is NOT in, so a small table has no scale to judge
#: a score against: under this many rows the floor does not apply.
RECALL_WEAK_MIN_ROWS = 200
#: ``mem_provenance.kind`` values with no age limit in an owner-chat recall (an
#: address, a handle, a contract id, a preference). The absence of a mark proves
#: nothing: run narration has no mark either, so every other kind is episodic.
DURABLE_KINDS = ("fact",)
#: A line that holds this marker is a claim the run could not check
#: (``memory_writer`` / ``reflection_service``). An owner chat never gets it as
#: recall. ``strip_unverified`` is the one rule; a writer marks through
#: ``mark_unverified``.
UNVERIFIED_MARK = "[unverified]"

#: The usual reciprocal-rank constant.
_FUSE_K = 60

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


def weak_match_sql(row_count: int) -> tuple:
    """``(sql, args)`` that keeps a weak match out of an automatic recall, for a table
    of ``row_count`` rows. FTS5 ``rank`` is the negative bm25 score."""
    if row_count < RECALL_WEAK_MIN_ROWS:
        return "", ()
    return " AND rank <= ?", (-RECALL_WEAK_SCORE,)


def mark_unverified(text: str) -> str:
    """``text`` with the marker on each line: the filter reads per line, so a mark
    on line 1 alone let the other lines of a multi-line summary through."""
    return "\n".join(ln if not ln.strip() or ln.startswith(UNVERIFIED_MARK)
                     else f"{UNVERIFIED_MARK} {ln}" for ln in (text or "").split("\n"))


def strip_unverified(content: str) -> str:
    """``content`` without a line that holds the marker, at any place in the line
    ("" when no finding is left). A ``User: …\\nAssistant: …`` row (the server
    default) is judged on its findings: a row with only its question left is empty."""
    content = content or ""
    head, sep, body = (content.partition("\nAssistant: ") if content.startswith("User: ")
                       else ("", "", content))
    body = "\n".join(ln for ln in body.split("\n") if UNVERIFIED_MARK not in ln).strip()
    return f"{head}{sep}{body}" if body else ""


def unverified_row_sql() -> tuple:
    """``(sql, args)`` that keeps a row with no line left under ``strip_unverified``
    out of the candidate pool. Only a one-line row can be judged in SQL; a longer
    row goes through ``strip_unverified`` line by line."""
    return (" AND NOT (instr(m.content, ?) > 0 AND instr(m.content, char(10)) = 0)",
            (UNVERIFIED_MARK,))


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


def fuse_ranked(first: List[dict], second: List[dict], *, limit: int,
                demote_automation: bool = True) -> List[dict]:
    """Reciprocal-rank fusion of two ranked lists of one store (rows carry ``rowid``). The
    reader's group order holds over the fused list, and of two near-duplicates the better
    placed one stays, as each list already kept the newer of its own."""
    scores, rows = {}, {}
    for ranked in (first, second):
        for i, r in enumerate(ranked):
            scores[r["rowid"]] = scores.get(r["rowid"], 0.0) + 1.0 / (_FUSE_K + i + 1)
            rows.setdefault(r["rowid"], r)
    order = sorted(scores, key=lambda rowid: (
        0 if rows[rowid].get("own") else 1,
        (1 if rows[rowid].get("auto") else 0) if demote_automation else 0,
        -scores[rowid]))
    kept: List[dict] = []
    for rowid in order:
        if not any(near_duplicate(k["content"], rows[rowid]["content"]) for k in kept):
            kept.append(rows[rowid])
    return kept[:limit]


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
