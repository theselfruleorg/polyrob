"""060 WS-3 — the RECORD guard the filesystem verbs call (2026-09-23).

``core.doc_kind`` decides; this module supplies the two facts the decision
needs from the tool tier: the file's current content and whether THIS session
already wrote it (the agent's own draft is never a record to it).

The per-session write set is module-level (a FileSystem instance can serve
several sessions) and bounded: the oldest session's set is dropped past
``_MAX_SESSIONS``, which can only make the guard STRICTER for that session
(an old draft reads as a record again), never looser.
"""
from __future__ import annotations

import logging
import os
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_SESSIONS = 256
_WRITES: "OrderedDict[str, set]" = OrderedDict()


def _key(session_id: Optional[str]) -> str:
    return str(session_id or "")


def mark_written(session_id: Optional[str], path: str) -> None:
    """Record that ``session_id`` wrote ``path`` (its own draft from now on)."""
    k = _key(session_id)
    if not k:
        return
    paths = _WRITES.pop(k, None) or set()
    paths.add(os.path.realpath(path))
    _WRITES[k] = paths
    while len(_WRITES) > _MAX_SESSIONS:
        _WRITES.popitem(last=False)


def wrote_this_session(session_id: Optional[str], path: str) -> bool:
    return os.path.realpath(path) in (_WRITES.get(_key(session_id)) or set())


def record_refusal(fs, normalized_path: str, display_path: str, root: str,
                   new_content: Optional[str]) -> Optional[str]:
    """The refusal sentence when this write/delete would edit a RECORD, else None.

    ``new_content`` None = a delete. An existing file that cannot be read is
    let through here (logged): the write verb itself reports that failure, and a
    guard that cannot read the history cannot claim to protect it.
    """
    from core.doc_kind import doc_kind_enforced, is_doc_path, record_write_refusal
    if not (doc_kind_enforced() and is_doc_path(normalized_path)):
        return None
    if not os.path.isfile(normalized_path):
        return None
    try:
        old = fs._safe_read_text(normalized_path, root)
    except Exception as e:
        logger.warning("record guard could not read %s (%s): write not guarded",
                       display_path, e)
        return None
    session_id = getattr(fs, "session_id", None)
    return record_write_refusal(display_path, old, new_content,
                                written_this_session=wrote_this_session(session_id, normalized_path))


__all__ = ["mark_written", "wrote_this_session", "record_refusal"]
