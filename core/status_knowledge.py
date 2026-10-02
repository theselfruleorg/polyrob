"""The ``knowledge`` status section — what the agent KNOWS and has WRITTEN.

The 2026-09-22 knowledge and record coherency review measured prod and found the snapshot had nineteen
sections and not one of them was about knowledge: 19,001 recall rows, 1,836
episodes, 76 indexed sources, 473 registered documents and 2,449 session trees
were reported on no seat at all. "How am I doing" could not answer "what do I
know".

Five stores, one section. Each is read independently and answers one of THREE
things, never two:

- a **count** — the file exists and was read;
- **not recorded yet** — the file is absent. A fresh install has no
  ``memory.db``; that is not a fault and raises no health item;
- **unreadable (<reason>)** — the file exists and the read FAILED. That is a
  health item, because a store that refuses to answer must never render as the
  zero it looks like (the 2026-08-28 rule this whole snapshot exists for).

Layering: ``core`` may not import ``agents.*`` or the tools tier, so every store
is read through its SQLite file with ``core.status_snapshot._rows`` (read-only,
existence-guarded — a status read never CREATES a database) and the session
tree through ``core.runtime_paths.resolve_session_data_root``.

⚠️ The session count is a ``scandir`` of one directory, not a ``du``. Prod holds
2,449 trees over 5.0 GB; sizing them on every ``/status`` would turn a cheap
read into a disk walk. The count and the ages are the facts that matter — the
bytes follow from them.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional

from core.runtime_paths import data_dir_or_home, resolve_session_data_root
from core.status_snapshot import (
    SEVERITY_WARN, HealthItem, Section, _rows,
)

#: Fallback when the retention setting cannot be read — the section still
#: reports honestly on a build where the sweep is not installed.
SESSION_AGE_DAYS = 90


def _session_window() -> tuple:
    """``(days, swept)`` — the retention window and whether a sweep enforces it."""
    try:
        from core.session_retention import retention_days
        days = int(retention_days())
    except Exception:
        return SESSION_AGE_DAYS, False
    if days <= 0:
        return SESSION_AGE_DAYS, False
    return days, True

#: Enough to show the shape of the collection set without turning one status
#: line into a listing.
_MAX_COLLECTIONS = 4

#: WS-K3 health thresholds. Below the floor the ratio is noise (two documents
#: and one indexed is not a finding); the share is the line prod sat far under
#: on 2026-09-22 — 76 of 473, 16%.
MIN_DOCS_FOR_INDEX_HEALTH = 20
MIN_INDEXED_SHARE = 0.25


def _why(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:120]}".strip()


def _memory_db(data_dir: str) -> str:
    """The memory home, resolved exactly as ``backend_factory`` resolves it."""
    return os.path.join(data_dir_or_home(data_dir), "memory.db")


def _sidecar(data_dir: str, name: str) -> str:
    return os.path.join(data_dir_or_home(data_dir), name)


def _probe(sec: Section, store: str, fn: Callable[[], Any]) -> Optional[Any]:
    """Run one store read and record which of the three answers it gave.

    Returns the value, or ``None`` when the store was absent or unreadable —
    and in the unreadable case leaves a health item behind, so the caller may
    simply skip the line rather than invent a number for it.
    """
    try:
        return fn()
    except FileNotFoundError:
        sec.data[store] = {"state": "absent"}
        return None
    except Exception as exc:
        reason = _why(exc)
        sec.data[store] = {"state": "error", "reason": reason}
        sec.lines.append(f"{store}: unreadable ({reason})")
        sec.health.append(HealthItem(
            key="knowledge_unreadable",
            text=f"knowledge store {store} is unreadable ({reason})",
            remedy="polyrob doctor",
            severity=SEVERITY_WARN))
        return None


def _one(db: str, sql: str, params: tuple = ()) -> Dict[str, Any]:
    out = _rows(db, sql, params)
    return out[0] if out else {}


def _pct(part: int, whole: int) -> str:
    if not whole:
        return "?"
    return f"{round(100.0 * part / whole)}%"


def _day(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))


def _recall(sec: Section, mem_db: str, uid: str) -> None:
    row = _probe(sec, "recall", lambda: _one(
        mem_db, "SELECT count(*) AS n FROM memories WHERE user_id = ?", (uid,)))
    if row is None:
        return
    n = int(row.get("n") or 0)
    sec.data["recall"] = {"state": "ok", "rows": n}
    sec.lines.append(f"recall: {n:,} rows")


def _curated(sec: Section, mem_db: str, uid: str) -> None:
    row = _probe(sec, "curated", lambda: _one(
        mem_db, "SELECT count(*) AS n FROM curated_memory WHERE user_id = ?", (uid,)))
    if row is None:
        return
    n = int(row.get("n") or 0)
    sec.data["curated"] = {"state": "ok", "rows": n}
    raw = int((sec.data.get("recall") or {}).get("rows") or 0)
    note = " — nothing distilled yet" if (n == 0 and raw) else ""
    sec.lines.append(f"curated: {n:,} notes{note}")


def _episodes(sec: Section, mem_db: str, uid: str) -> None:
    rows = _probe(sec, "episodes", lambda: _rows(
        mem_db,
        "SELECT kind, count(*) AS n, sum(surfaced) AS s, max(ts) AS newest "
        "FROM episodes WHERE user_id = ? GROUP BY kind", (uid,)))
    if rows is None:
        return
    total = sum(int(r.get("n") or 0) for r in rows)
    surfaced = sum(int(r.get("s") or 0) for r in rows)
    newest = max([r.get("newest") or 0 for r in rows] or [0])
    by_kind = {str(r.get("kind") or "?"): int(r.get("n") or 0) for r in rows}
    sec.data["episodes"] = {"state": "ok", "rows": total, "surfaced": surfaced,
                            "by_kind": by_kind, "newest": newest or None}
    if not total:
        sec.lines.append("episodes: none recorded")
        return
    shape = " · ".join(f"{k} {v:,}" for k, v in sorted(
        by_kind.items(), key=lambda kv: -kv[1])[:3])
    sec.lines.append(
        f"episodes: {total:,} ({shape}), {_pct(surfaced, total)} surfaced, "
        f"newest {_day(newest)}")


def _kb(sec: Section, mem_db: str, uid: str) -> None:
    rows = _probe(sec, "kb", lambda: _rows(
        mem_db,
        "SELECT collection, count(*) AS n FROM kb_sources WHERE user_id = ? "
        "GROUP BY collection ORDER BY n DESC", (uid,)))
    if rows is None:
        return
    total = sum(int(r.get("n") or 0) for r in rows)
    names = [str(r.get("collection") or "?") for r in rows]
    sec.data["kb"] = {"state": "ok", "sources": total, "collections": names}
    if not total:
        sec.lines.append("knowledge base: no sources indexed")
        return
    shown = ", ".join(names[:_MAX_COLLECTIONS])
    more = f" +{len(names) - _MAX_COLLECTIONS}" if len(names) > _MAX_COLLECTIONS else ""
    sec.lines.append(f"knowledge base: {total:,} sources in {len(names)} "
                     f"collection(s) ({shown}{more})")


def _documents(sec: Section, data_dir: str, uid: str) -> None:
    """What the agent has WRITTEN, from the artifact registry it already keeps.

    ⚠️ The indexed RATIO counts text documents only. Comparing 76 indexed
    sources against every artifact — screenshots, JSON, video — reports a gap
    that no ingest could ever close, which is the confident-but-wrong shape
    this whole section exists to avoid.
    """
    from core.artifacts import TEXT_DOCUMENT_SUFFIXES

    suffixes = sorted(TEXT_DOCUMENT_SUFFIXES)
    doc_clause = " OR ".join("lower(path) LIKE ?" for _ in suffixes)
    row = _probe(sec, "documents", lambda: _one(
        _sidecar(data_dir, "artifacts.db"),
        f"SELECT count(*) AS n, sum(CASE WHEN {doc_clause} THEN 1 ELSE 0 END) "
        "AS docs FROM artifacts WHERE user_id = ?",
        tuple(f"%{sfx}" for sfx in suffixes) + (uid,)))
    if row is None:
        return
    n = int(row.get("n") or 0)
    docs = int(row.get("docs") or 0)
    kb = sec.data.get("kb") or {}
    indexed = int(kb.get("sources") or 0)
    sec.data["documents"] = {"state": "ok", "rows": n, "text": docs,
                             "indexed": indexed}
    if not n:
        sec.lines.append("documents: none registered")
        return
    sec.lines.append(f"documents: {n:,} registered ({docs:,} text), "
                     f"{indexed:,} indexed ({_pct(indexed, docs)})")
    # WS-K3. Only when the KB itself answered: an instance with no knowledge
    # base is not failing to index, it simply has nowhere to index TO.
    if kb.get("state") == "ok" and docs >= MIN_DOCS_FOR_INDEX_HEALTH and \
            indexed < docs * MIN_INDEXED_SHARE:
        sec.health.append(HealthItem(
            key="knowledge_unindexed",
            text=(f"{docs - indexed:,} of {docs:,} written documents are not in "
                  f"the knowledge base"),
            remedy="polyrob kb reindex",
            severity=SEVERITY_WARN))


def _skills(sec: Section, data_dir: str, uid: str) -> None:
    rows = _probe(sec, "skills", lambda: _rows(
        _sidecar(data_dir, "skill_usage.db"),
        "SELECT skill_id, load_count, last_used_at FROM skill_usage "
        "WHERE user_id = ? AND load_count > 0", (uid,)))
    if rows is None:
        return
    used = len(rows)
    last = max([r.get("last_used_at") or 0 for r in rows] or [0])
    sec.data["skills"] = {"state": "ok", "ever_loaded": used, "last_used": last or None}
    if not used:
        sec.lines.append("skills: none loaded yet")
        return
    sec.lines.append(f"skills: {used} ever loaded, last {_day(last)}")


def _sessions(sec: Section, now: float) -> None:
    """Session trees by count and age — a scandir, never a size walk."""
    window, swept = _session_window()

    def read() -> Dict[str, int]:
        root = resolve_session_data_root()
        if not root.exists():
            raise FileNotFoundError(f"session root not found at {root}")
        cutoff = now - window * 86400
        total = old = 0
        oldest: Optional[float] = None
        for tenant in os.scandir(root):
            if not tenant.is_dir():
                continue
            for entry in os.scandir(tenant.path):
                if not entry.is_dir():
                    continue
                total += 1
                mtime = entry.stat().st_mtime
                if oldest is None or mtime < oldest:
                    oldest = mtime
                if mtime < cutoff:
                    old += 1
        return {"trees": total, "older": old, "oldest": int(oldest or 0)}

    got = _probe(sec, "sessions", read)
    if got is None:
        return
    got["state"] = "ok"
    sec.data["sessions"] = got
    if not got["trees"]:
        sec.lines.append("sessions: none on disk")
        return
    got["retention_days"] = window if swept else 0
    tail = (f", {got['older']:,} older than {window}d"
            if got["older"] else "")
    sec.lines.append(f"sessions: {got['trees']:,} trees{tail}, "
                     f"oldest {_day(got['oldest'])}"
                     + ("" if swept else " — no retention policy"))
    # WS-K2. Only when a sweep is supposed to be enforcing the window: an owner
    # who turned retention off is not failing to retain, they decided not to.
    if swept and got["older"]:
        sec.health.append(HealthItem(
            key="sessions_unretained",
            text=(f"{got['older']:,} session trees are older than the "
                  f"{window}-day window"),
            remedy="polyrob sessions prune",
            severity=SEVERITY_WARN))


def _headline(sec: Section) -> str:
    """The one line a seat shows when it shows only one."""
    d = sec.data
    def n(store: str, key: str = "rows") -> str:
        got = d.get(store) or {}
        if got.get("state") == "error":
            return "?"
        if got.get("state") != "ok":
            return "—"
        return f"{int(got.get(key) or 0):,}"
    return (f"{n('recall')} recalled · {n('episodes')} episodes · "
            f"{n('curated')} curated · {n('kb', 'sources')} indexed · "
            f"{n('documents')} written")


def knowledge_section(user_id: str, data_dir: str,
                      now: Optional[float] = None) -> Section:
    """What this tenant knows, remembers, has indexed and has written."""
    now = float(now if now is not None else time.time())
    uid = str(user_id or "")
    sec = Section(name="knowledge", data={})
    mem_db = _memory_db(data_dir)

    _recall(sec, mem_db, uid)
    _curated(sec, mem_db, uid)
    _episodes(sec, mem_db, uid)
    _kb(sec, mem_db, uid)
    _documents(sec, data_dir, uid)
    _skills(sec, data_dir, uid)
    _sessions(sec, now)

    detail: List[str] = list(sec.lines)
    if not detail:
        sec.lines = ["nothing recorded yet"]
        return sec
    sec.lines = [_headline(sec)] + detail
    return sec


__all__ = ["knowledge_section", "SESSION_AGE_DAYS"]
