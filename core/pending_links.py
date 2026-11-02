"""Two pending drafts that are ONE owner decision (095 W10, design rule R8).

When ``skill_manage`` changes the terms of a programme in that lane's pinned
skill, it also drafts the retire of the ``owner.md`` line on the same subject
(the old line moves under ``## Superseded``; it is never deleted). The owner
must not be able to promote one half: a new term in the skill beside the old
owner.md line is the 2026-10-28 "three disagreeing versions" again. So the two
drafts are LINKED here, and ``core.self_evolution`` promotes or rejects both on
one tap and shows them as one item.

A link is a pair of ``kind:id`` keys in ``.pending_links.json`` under the
tenant's identity tier root (beside the pending notify state). Fail-open: an
unreadable link file means "no links" and each draft is decided alone, which is
the behaviour before links existed.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_LINKS_FILE = ".pending_links.json"


def _key(kind: str, item_id) -> str:
    return f"{kind}:{item_id}"


def _split(key: str) -> Tuple[str, str]:
    kind, _, item_id = key.partition(":")
    return kind, item_id


def _path(user_id: str, home_dir, instance_id: str) -> Optional[Path]:
    try:
        from core.instance import is_safe_tenant_id, self_tier_root
        if not is_safe_tenant_id(user_id):
            return None
        return self_tier_root(home_dir, user_id, instance_id) / _LINKS_FILE
    except Exception:
        return None


def _load(path: Optional[Path]) -> Dict[str, str]:
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("pending links unreadable at %s — each draft is decided alone", path)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save(path: Optional[Path], links: Dict[str, str]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(links, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.warning("pending link not saved (%s) — each draft is decided alone", e)


def link(user_id: str, a: Tuple[str, str], b: Tuple[str, str], *, home_dir,
         instance_id: str) -> None:
    """Make drafts ``a`` and ``b`` (``(kind, id)``) one decision."""
    path = _path(user_id, home_dir, instance_id)
    links = _load(path)
    ka, kb = _key(*a), _key(*b)
    for k in (ka, kb):  # a draft belongs to one pair only
        old = links.pop(k, None)
        if old:
            links.pop(old, None)
    links[ka], links[kb] = kb, ka
    _save(path, links)


def partner(user_id: str, kind: str, item_id, *, home_dir,
            instance_id: str) -> Optional[Tuple[str, str]]:
    """The ``(kind, id)`` linked to this draft, or None."""
    other = _load(_path(user_id, home_dir, instance_id)).get(_key(kind, item_id))
    return _split(other) if other else None


def unlink(user_id: str, kind: str, item_id, *, home_dir, instance_id: str) -> None:
    """Drop the pair this draft is in (it was decided)."""
    path = _path(user_id, home_dir, instance_id)
    links = _load(path)
    other = links.pop(_key(kind, item_id), None)
    if other is None:
        return
    links.pop(other, None)
    _save(path, links)


__all__ = ["link", "partner", "unlink"]
