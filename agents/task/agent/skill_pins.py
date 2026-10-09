"""Content digests of a row's PINNED skills, bound at ``/adopt``.

A cron job or goal pins doctrine by skill id (``payload.skills``). Adoption makes
the row OWNER-authored after the owner saw what it does — but a skill is text the
agent can edit later (``skill_manage``), so the doctrine the row runs would no
longer be what the owner adopted. ``/adopt`` therefore shows each pinned skill
with a digest of its files and stores the combined digest on the row
(``ADOPTED_SKILLS_KEY``); every run recomputes it, and a row whose pinned skills
changed runs as AGENT-authored again until the owner re-adopts it
(:func:`adoption_lapsed`).
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: The combined digest of the pinned skills the owner adopted.
ADOPTED_SKILLS_KEY = "adopted_skills_digest"
#: Marked on a run's (in-memory) payload whose adopted skills changed.
LAPSED_KEY = "adoption_lapsed"

_MAX_FILES = 400
_MAX_BYTES = 8 * 1024 * 1024
MISSING = "missing"


def _dir_digest(root: str) -> str:
    """sha256 over every regular file under *root* (relative path + bytes),
    sorted; symlinks are named, never followed. Bounded."""
    h = hashlib.sha256()
    seen = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            seen += 1
            if seen > _MAX_FILES:
                h.update(b"\0too-many-files")
                return h.hexdigest()
            if os.path.islink(path):
                h.update(f"L:{rel}->{os.readlink(path)}\n".encode())
                continue
            try:
                with open(path, "rb") as fh:
                    data = fh.read(_MAX_BYTES - total + 1)
            except OSError:
                h.update(f"E:{rel}\n".encode())
                continue
            total += len(data)
            h.update(f"F:{rel}:{len(data)}\n".encode())
            h.update(data)
            if total > _MAX_BYTES:
                h.update(b"\0too-large")
                return h.hexdigest()
    return h.hexdigest()


def skill_digests(skill_ids: List[str], user_id: Optional[str] = None
                  ) -> List[Tuple[str, str]]:
    """``[(skill_id, sha256 hex | "missing")]`` in the order given."""
    out: List[Tuple[str, str]] = []
    try:
        from agents.task.agent.skill_manager import get_skill_manager
        mgr = get_skill_manager()
    except Exception:
        logger.debug("skill_pins: skill manager unavailable", exc_info=True)
        mgr = None
    for sid in skill_ids:
        where = None
        if mgr is not None:
            try:
                where = mgr.resolve_skill_dir(sid, user_id=user_id)
            except Exception:
                where = None
        out.append((sid, _dir_digest(str(where)) if where and os.path.isdir(str(where))
                    else MISSING))
    return out


def combined(digests: List[Tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for sid, dg in digests:
        h.update(f"{sid}={dg}\n".encode())
    return h.hexdigest()


def pinned_digest(payload: Optional[Mapping], user_id: Optional[str] = None
                  ) -> Optional[str]:
    """The combined digest of *payload*'s pinned skills, or None when it pins none."""
    from core.config_policy.rigs import pinned_skills
    ids = pinned_skills(payload)
    if not ids:
        return None
    return combined(skill_digests(ids, user_id))


def adoption_lapsed(payload: Optional[Mapping], user_id: Optional[str] = None) -> bool:
    """True when an ADOPTED row's pinned skills are not the ones the owner saw.

    Only a row that carries :data:`ADOPTED_SKILLS_KEY` is checked; a row the
    owner created himself never was adopted. Fail-closed: a digest that cannot
    be computed counts as changed."""
    if not isinstance(payload, Mapping):
        return False
    bound = payload.get(ADOPTED_SKILLS_KEY)
    if not bound:
        return False
    try:
        return pinned_digest(payload, user_id) != bound
    except Exception:
        logger.warning("skill_pins: digest failed — adoption treated as lapsed",
                       exc_info=True)
        return True


def lapse_payload(payload: Mapping) -> Dict:
    """*payload* as AGENT-authored for this run (the stored row is unchanged —
    ``/adopt`` lists it again)."""
    from core.config_policy.rigs import AGENT_AUTHOR, AUTHORED_BY_KEY
    out = {**dict(payload), AUTHORED_BY_KEY: AGENT_AUTHOR, LAPSED_KEY: True}
    out.pop("owner_granted", None)
    return out


__all__ = ["ADOPTED_SKILLS_KEY", "LAPSED_KEY", "MISSING", "adoption_lapsed", "combined",
           "lapse_payload", "pinned_digest", "skill_digests"]
