"""Why the agent drafted a pending proposal (096 W1).

No drafter recorded WHY: a skill draft, an identity / owner-facts / contract
draft, a guarded pref change carried no rationale, and the background reviewer
put the run it learned from only into an event note. So no owner seat could
ever answer "why do you want this?" — the notice described the new text and
nothing else.

One store for every pending kind, beside ``core.pending_links`` (the same tier
root, the same fail-open file shape): ``.pending_reasons.json`` maps
``kind:id`` to a short list of ``{reason, by, at}``. A draft revised by several
turns keeps the last few reasons, newest last. ``core.self_evolution`` reads it
into each ``list_pending`` item (``reason``/``reasons``/``reason_at``) and
forgets the entry when the owner decides the item, so a later draft under the
same id starts clean.

Fail-open: an unreadable file means "no reason recorded" — the seat says so;
it never blocks a draft or a decision.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_FILE = ".pending_reasons.json"
#: One sentence. Longer text is cut here, at the source, with a marker.
REASON_MAX_CHARS = 300
#: A draft revised many times keeps only its newest reasons.
_KEEP = 3


def _key(kind: str, item_id) -> str:
    return f"{kind}:{item_id}"


def _path(user_id: str, home_dir, instance_id: str) -> Optional[Path]:
    try:
        from core.instance import is_safe_tenant_id, self_tier_root
        if not is_safe_tenant_id(user_id):
            return None
        return self_tier_root(home_dir, user_id, instance_id) / _FILE
    except Exception:
        return None


#: ``all_reasons`` carries this key when the store exists but cannot be read:
#: a seat says "unreadable", never "the draft gave no reason".
UNREADABLE = "__unreadable__"


class ReasonsUnreadable(Exception):
    """The reasons file exists but is not readable JSON. A writer must NOT
    replace it (every other draft would lose its reason)."""


def _load(path: Optional[Path]) -> Dict[str, list]:
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("pending reasons unreadable at %s (%s) — not overwritten", path, e)
        raise ReasonsUnreadable(str(e)) from e
    if not isinstance(data, dict):
        logger.warning("pending reasons at %s is not a JSON object — not overwritten", path)
        raise ReasonsUnreadable("not a JSON object")
    return {str(k): [e for e in v if isinstance(e, dict)]
            for k, v in data.items() if isinstance(v, list)}


def _save(path: Optional[Path], data: Dict[str, list]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.warning("pending reason not saved (%s)", e)


def clean_reason(text) -> str:
    """One line, at most ``REASON_MAX_CHARS`` (cut with a marker). Pure."""
    out = " ".join(str(text or "").split())
    if len(out) > REASON_MAX_CHARS:
        out = out[:REASON_MAX_CHARS - 1].rstrip() + "…"
    return out


def record(user_id: str, kind: str, item_id, reason, *, by: str = "",
           home_dir, instance_id: str, only_if_missing: bool = False) -> None:
    """Add *reason* to the draft ``kind:id``. A repeat of the newest reason is
    not added twice. ``only_if_missing`` adds it only when none is recorded
    (the background reviewer's fallback). Never raises."""
    try:
        text = clean_reason(reason)
        if not text:
            return
        path = _path(user_id, home_dir, instance_id)
        data = _load(path)
        entries = data.get(_key(kind, item_id), [])
        if only_if_missing and entries:
            return
        if entries and entries[-1].get("reason") == text:
            return
        entries.append({"reason": text, "by": str(by or ""), "at": int(time.time())})
        data[_key(kind, item_id)] = entries[-_KEEP:]
        _save(path, data)
    except Exception:
        logger.warning("pending reason not recorded", exc_info=True)


def reasons(user_id: str, kind: str, item_id, *, home_dir,
            instance_id: str) -> List[dict]:
    """The recorded reasons for ``kind:id``, oldest first. ``[]`` when none."""
    try:
        return list(_load(_path(user_id, home_dir, instance_id)).get(_key(kind, item_id), []))
    except Exception:
        return []


def all_reasons(user_id: str, *, home_dir, instance_id: str) -> Dict[str, list]:
    """Every recorded entry for the tenant (one file read for a whole list).
    An unreadable store returns ``{UNREADABLE: []}``."""
    try:
        return _load(_path(user_id, home_dir, instance_id))
    except ReasonsUnreadable:
        return {UNREADABLE: []}
    except Exception:
        return {}


def forget(user_id: str, kind: str, item_id, *, home_dir, instance_id: str) -> None:
    """Drop ``kind:id`` (the owner decided it). Never raises."""
    try:
        path = _path(user_id, home_dir, instance_id)
        data = _load(path)
        if data.pop(_key(kind, item_id), None) is not None:
            _save(path, data)
    except Exception:
        logger.debug("pending reason not forgotten", exc_info=True)
