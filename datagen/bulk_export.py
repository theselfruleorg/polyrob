"""Bulk label-filtered corpus export over a data root (design spec §A1).

Walks ``<data_root>/<user_id>/sessions/<session_id>/`` session dirs, assembles
each into a canonical record (episode-label-enriched when ``memory.db`` has a
row), applies label filters, scrubs fail-closed, and appends one JSONL line
per surviving session. A scrub failure skips THAT session (counted) — a bulk
run never exports unverified content and never aborts the whole corpus.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator, Optional

from datagen.assemble import assemble_record, find_memory_db, load_episode_labels
from datagen.formats import FORMATS
from datagen.scrub import ScrubError, has_correspondent_content, scrub_record

logger = logging.getLogger(__name__)

#: A session dir is recognized by its persisted artifacts, not its depth —
#: the canonical walk would otherwise yield tenant/infra dirs as "sessions".
_SESSION_MARKERS = ("metadata.json", "memory", "data")


def _looks_like_session(p: Path) -> bool:
    return any((p / marker).exists() for marker in _SESSION_MARKERS)


def iter_session_dirs(data_root: Path) -> Iterator[tuple[str, str, Path]]:
    """Yield ``(user_id, session_id, session_dir)`` under a data root.

    Canonical live layout FIRST: ``<data_root>/<user_id>/<session_id>/`` —
    ``pm().get_session_root`` creates sessions directly under the user (no
    ``sessions/`` subdir; server ``data/task/...`` and local
    ``<home>/sessions/...`` both resolve this way). The legacy
    ``*/sessions/*`` and flat ``sessions/*`` layouts remain as fallbacks."""
    data_root = Path(data_root)
    seen: set = set()
    for p in sorted(data_root.glob("*/*")):
        if not p.is_dir() or p.name == "sessions" or p.parent.name == "sessions":
            continue
        if not _looks_like_session(p):
            continue
        seen.add(str(p))
        yield (p.parent.name, p.name, p)
    for p in sorted(data_root.glob("*/sessions/*")):
        if p.is_dir() and str(p) not in seen:
            seen.add(str(p))
            yield (p.parent.parent.name, p.name, p)
    for p in sorted(data_root.glob("sessions/*")):  # flat, userless layout
        if p.is_dir() and _looks_like_session(p) and str(p) not in seen:
            seen.add(str(p))
            yield ("", p.name, p)
    # Legacy ``data/auto/<user>/sessions/<sid>`` lives at a SIBLING of the data
    # root (``data/task``), so no walk under data_root can reach it (H6,
    # 2026-07-14 review) — those sessions were silently excluded from corpora.
    legacy_auto = data_root.parent / "auto"
    if legacy_auto.is_dir() and legacy_auto.resolve() != data_root.resolve():
        for p in sorted(legacy_auto.glob("*/sessions/*")):
            if p.is_dir() and _looks_like_session(p) and str(p) not in seen:
                seen.add(str(p))
                yield (p.parent.parent.name, p.name, p)


def _passes_filters(labels: dict, filters: dict) -> bool:
    for key, want in (filters or {}).items():
        if str(labels.get(key, "")).lower() != str(want).lower():
            return False
    return True


def bulk_export(data_root: Path, out_path: Path, fmt: str, filters: dict,
                *, include_correspondent: bool = False,
                limit: Optional[int] = None,
                user_id: Optional[str] = None) -> dict:
    """Export matching sessions as JSONL. Returns counters."""
    data_root = Path(data_root)
    render = FORMATS[fmt]
    memory_db = find_memory_db(data_root) or (data_root / "memory.db")
    stats = {"exported": 0, "skipped_filter": 0,
             "skipped_correspondent": 0, "skipped_scrub": 0,
             "skipped_empty": 0}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as out:
        for uid, sid, sdir in iter_session_dirs(data_root):
            if limit is not None and stats["exported"] >= limit:
                break
            if user_id is not None and uid != user_id:
                continue
            labels = load_episode_labels(memory_db, uid, sid)
            record = assemble_record(sdir, labels=labels, user_id=uid)
            if not record.messages:
                stats["skipped_empty"] += 1
                continue
            if not _passes_filters(record.labels, filters):
                stats["skipped_filter"] += 1
                continue
            if not include_correspondent and has_correspondent_content(record):
                stats["skipped_correspondent"] += 1
                continue
            try:
                scrub_record(record)
            except ScrubError:
                logger.warning("datagen: scrub failed for %s/%s — skipped",
                               uid, sid)
                stats["skipped_scrub"] += 1
                continue
            out.write(json.dumps(render(record), default=str) + "\n")
            stats["exported"] += 1
    return stats
