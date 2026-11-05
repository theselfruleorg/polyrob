"""`/skills` on the chat seats (080 D8) — the skills the agent can load.

The listing is :func:`catalog`, the ONE reader the REPL's ``/skills`` renders
too (``cli/ui/commands/h_skills.py``), so a phone and a terminal list the same
skills with the same words. Read-only here: installing, approving and removing
a skill stay local-operator verbs (``polyrob skill …``, REPL ``/skills …``).
Each listed skill carries its read tap ``/skill_<id>`` (096 C7), which opens
the live body.
"""
from typing import List, Tuple

#: Bound on the catalog read; the library is a few dozen skills.
MAX_SKILLS = 200
#: Description width on one listed line.
DESC_CHARS = 80


def _short(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def catalog(user_id: str, query: str = "", *,
            desc_chars: int = DESC_CHARS) -> List[Tuple[str, str]]:
    """``[(skill id, one-line description)]`` of the loadable skills, sorted by
    id, filtered by *query* over id + description. Raises when the skill
    store cannot be read (a seat says so — never "no skills")."""
    from agents.task.agent.skill_manager import get_skill_manager
    skills = get_skill_manager().get_catalog_skills(
        user_id=user_id or "local", max_skills=MAX_SKILLS)
    q = (query or "").strip().lower()
    rows = []
    for s in skills or []:
        sid = str(getattr(s, "skill_id", "") or "?")
        desc = str(getattr(s, "description", "") or "")
        if q and q not in sid.lower() and q not in desc.lower():
            continue
        rows.append((sid, _short(desc, desc_chars)))
    return sorted(rows)


def unreadable_note() -> str:
    """The line a listing adds for a skill source it could not read, or ""."""
    from core.skills.scopes import unreadable_sources
    unread = unreadable_sources()
    if not unread:
        return ""
    return ("I could not read skills from " + ", ".join(unread)
            + ". That is not 'no skills' there.")


def _fold(skill_id: str) -> str:
    """The tap form of an id (``core.surfaces.tappable.read_token``): ``-`` → ``_``."""
    return str(skill_id or "").lower().replace("-", "_")


def _exact(user_id: str, word: str) -> Tuple[List[str], bool]:
    """``(ids, read)`` — the live skill ids one word names EXACTLY, either as
    typed or in its folded tap form. ``read`` is False when the store cannot be
    read (the caller then lists instead)."""
    if not word or " " in word:
        return [], True
    try:
        ids = [sid for sid, _d in catalog(user_id, "", desc_chars=1)]
    except Exception:  # noqa: BLE001 — fall through to the listing's own error
        return [], False
    if word in ids:
        return [word], True
    folded = _fold(word)
    return [sid for sid in ids if _fold(sid) == folded], True


def skill_body_reply(user_id: str, skill_id: str) -> str:
    """`/skills <id>` or the tapped `/skill_<id>` (096 C7): the LIVE skill
    body — what a session loads, frontmatter off. A read; changing a skill goes
    through the agent and /pending. A long body is the reply's own business
    (the seat spills it); it is never cut here."""
    from agents.task.agent.skill_manager import get_skill_manager
    try:
        body = get_skill_manager()._load_skill_content(skill_id, user_id or "local")
    except Exception as exc:  # noqa: BLE001 — an unread store is not an empty skill
        return f"I could not read skill '{skill_id}' ({type(exc).__name__})."
    if not (body or "").strip():
        return (f"Skill '{skill_id}' has no body I can load (it may be refused by "
                "its integrity check) — see /skills.")
    return f"📘 Skill {skill_id} — the live text:\n\n{body.strip()}"


def skills_reply(user_id: str, args: list) -> str:
    """`/skills [query | id]` — the loadable skills, one line each, each with
    its read tap; a word that IS a skill id opens that skill's body."""
    query = " ".join(str(a) for a in (args or [])).strip()
    ids, _ok = _exact(user_id, query)
    if len(ids) == 1:
        return skill_body_reply(user_id, ids[0])
    if len(ids) > 1:
        return (f"'{query}' names {len(ids)} skills: " + ", ".join(ids)
                + " — type the id with its dashes.")
    try:
        rows = catalog(user_id, query)
    except Exception as exc:  # noqa: BLE001 — an unread store is not "none"
        return f"I could not read the skills ({type(exc).__name__}). That is not 'no skills'."
    note = unreadable_note()
    if not rows:
        return "\n".join(filter(None, (f"No skill matches '{query}'." if query
                                       else "No skills are installed.", note)))
    from core.surfaces.tappable import read_token
    lines = [f"{len(rows)} skill(s) I can load:"]
    for sid, desc in rows:
        tap = read_token("/skill", sid)
        lines.append(f"• {sid}" + (f" — {desc}" if desc else "")
                     + (f" · {tap}" if tap else ""))
    lines.append("Skills waiting for your review are in /pending.")
    if note:
        lines.append(note)
    return "\n".join(lines)
