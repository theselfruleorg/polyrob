"""`/skills` on the chat seats (080 D8) — the skills the agent can load.

The listing is :func:`catalog`, the ONE reader the REPL's ``/skills`` renders
too (``cli/ui/commands/h_skills.py``), so a phone and a terminal list the same
skills with the same words. Read-only here: installing, approving and removing
a skill stay local-operator verbs (``polyrob skill …``, REPL ``/skills …``).
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


def skills_reply(user_id: str, args: list) -> str:
    """`/skills [query]` — the loadable skills, one line each."""
    query = " ".join(str(a) for a in (args or [])).strip()
    try:
        rows = catalog(user_id, query)
    except Exception as exc:  # noqa: BLE001 — an unread store is not "none"
        return f"I could not read the skills ({type(exc).__name__}). That is not 'no skills'."
    if not rows:
        return (f"No skill matches '{query}'." if query
                else "No skills are installed.")
    lines = [f"{len(rows)} skill(s) I can load:"]
    lines += [f"• {sid}" + (f" — {desc}" if desc else "") for sid, desc in rows]
    lines.append("Skills waiting for your review are in /pending.")
    return "\n".join(lines)
