"""Repair a skill write that stopped between its files (098 MO-17).

The writer's commit protocol is body → rule → revision record; a
hash-verifying loader refuses an active body whose revision is not committed.
That keeps a half-write from being CONSUMED, but nothing repaired it: a crash
between the body and the commit left a skill the loader refused for ever, with
no notice; a crash after a promote and before the draft's delete left the skill
both active and pending.

:func:`repair_uncommitted_skills` (run by the curator pass) fixes both:

- an active body whose hash is not the committed revision rolls back to the
  committed body from ``.archived/<id>/<committed>-SKILL.md``; without that
  archive the body moves to ``.pending/`` for owner review
  (``skill_uncommitted_quarantined`` event);
- a pending draft whose revision equals the active committed revision is
  removed (the promote already happened).

A skill with no recorded revision (legacy) is never touched. Fail-open.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _emit(kind: str, **attrs: Any) -> None:
    try:
        from core.event_log import emit
        emit(kind, source="skills.repair", attrs={k: str(v)[:200] for k, v in attrs.items()})
    except Exception:
        pass


def repair_uncommitted_skills(sm: Any, uid: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {"rolled_back": [], "quarantined": [], "draft_removed": []}
    try:
        root: Path = sm._user_root(uid)
    except Exception:
        return out
    if not root.is_dir():
        return out
    from core.security.confined_write import unlink_confined
    from modules.skills.skill_usage import get_skill_usage_store
    store = get_skill_usage_store()
    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        skill_id = skill_dir.name
        ok, _ = sm.validate_skill_id(skill_id)
        body_path = skill_dir / "SKILL.md"
        if not ok or not body_path.is_file():
            continue
        try:
            committed = store.get_revision(skill_id, uid)
        except Exception:
            continue                         # unreadable store: decide nothing
        if not committed:
            continue                         # legacy skill: never touched
        try:
            sha = sm._content_revision(sm._read_skill_text(body_path))
        except (OSError, UnicodeError):
            continue
        pending_dir = root / ".pending" / skill_id
        if sha == committed:
            draft = pending_dir / "SKILL.md"
            try:
                if draft.is_file() and sm._content_revision(sm._read_skill_text(draft)) == committed:
                    unlink_confined(draft, sm._user_dirs_root())
                    rev = sm._pending_revision_path(uid, skill_id)
                    if rev.exists():
                        unlink_confined(rev, sm._user_dirs_root())
                    out["draft_removed"].append(skill_id)
            except Exception:
                logger.debug("skill repair: draft check failed for %s/%s", uid, skill_id,
                             exc_info=True)
            continue
        archived = root / ".archived" / skill_id / f"{committed}-SKILL.md"
        try:
            if archived.is_file():
                sm._atomic_write(body_path, sm._read_skill_text(archived))
                out["rolled_back"].append(skill_id)
                _emit("skill_uncommitted_rolled_back", user_id=uid, skill_id=skill_id,
                      revision=committed[:12])
            elif not (pending_dir / "SKILL.md").exists():
                sm._atomic_write(pending_dir / "SKILL.md", sm._read_skill_text(body_path))
                sm._read_pending_revision(uid, skill_id, sm._read_skill_text(body_path))
                unlink_confined(body_path, sm._user_dirs_root())
                out["quarantined"].append(skill_id)
                _emit("skill_uncommitted_quarantined", user_id=uid, skill_id=skill_id)
            sm._invalidate_skill_cache(uid, skill_id)
        except Exception:
            logger.warning("skill repair failed for %s/%s", uid, skill_id, exc_info=True)
    if any(out.values()):
        logger.warning("skill repair for %s: %s", uid, {k: v for k, v in out.items() if v})
    return out
