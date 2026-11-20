"""`/persona` on an owner seat: which voice the agent runs with.

The report reads the truth the running process resolves: the gate, the owner's
persona setting (``persona_setting.py``, the one store every seat sets), the
operator's persona, the character file, what the model receives and what it never
receives. Every chat seat renders this ONE body (a seat holds no procedure); the
setter is :func:`agents.personality.persona_setting.persona_command`.

Owner copy names the verb (``/persona <name>``, ``/persona reset``), never an env
flag: an operator-only fact says "the operator's setting".
"""
import os

#: The rendered block is long; a chat reply shows its head and says so.
MAX_BLOCK_CHARS = 2500
#: The names the footer lists before it says how many more there are.
MAX_NAMES = 12


def persona_report(args=(), *, user_id=None, home_dir=None) -> str:
    """The owner-facing persona report. Never raises. ``args`` is kept for old
    callers; a seat that sets the persona calls ``persona_setting.persona_command``."""
    try:
        return _report(user_id, home_dir) + _footer()
    except Exception as e:  # an unreadable character must still answer
        return f"Persona: I could not read it ({type(e).__name__}: {e})."


def _footer() -> str:
    from agents.personality.persona_setting import persona_names
    from agents.task.templates import TEMPLATES
    names, unreadable = persona_names()
    shown = sorted(TEMPLATES) + [n for n in names if n not in TEMPLATES]
    more = len(shown) - MAX_NAMES
    lines = ["", "Change it for new chats: `/persona <name>` (or your own text) · "
                 "back to the default: `/persona reset`.",
             "Names: " + ", ".join(shown[:MAX_NAMES]) + (f" (+{more} more)" if more > 0 else "")]
    for d in unreadable:
        lines.append(f"⚠️ I could not read the characters directory {d}.")
    return "\n".join(lines)


def _report(user_id=None, home_dir=None) -> str:
    from agents.personality import persona_resolver as pr
    from agents.personality.persona_render import render_persona_block, stored_only_note
    from agents.personality.persona_setting import owner_selector

    try:
        from core.config_policy import task_personality_block_enabled
        gate = task_personality_block_enabled()
    except Exception:
        gate = False
    if not gate:
        return ("Persona: OFF. The operator's setting turns the character block off, so no "
                "character reaches the model in any chat. `/persona <name>` is saved, and "
                "applies when the operator turns it on.")

    owner, problem = owner_selector(user_id, home_dir)
    explicit = owner or (os.environ.get("POLYROB_PERSONA") or "").strip()
    source = "your /persona setting" if owner else "the operator's setting"
    lines: list = []
    if problem:
        lines.append(f"⚠️ {problem[0].upper()}{problem[1:]}.")
    data = None
    if explicit:
        data = pr.load_character_dict(explicit)
        if data is not None:
            lines.append(f"Persona: character '{explicit}' ({source})")
            lines.append(f"File: {pr.find_character_file(explicit)}")
        else:
            text = pr.resolve_persona_selector(explicit) or ""
            lines.append(f"Persona: set by {source} (a template or your own text, "
                         f"{len(text)} chars); no character file is in use.")
            return "\n".join(lines + ["", _clip(text)])
    else:
        name, found = pr.active_character()
        if found is None:
            return "\n".join(lines + [
                f"Persona: character '{name}' — NO file found in any characters "
                "directory, and no packaged fallback. No character reaches the model."])
        fallback = found.name != f"{name}.character.json"
        lines.append(f"Persona: character '{name}' (the default)")
        if fallback:
            lines.append(f"⚠️ '{name}' is not in any characters directory: the agent "
                         f"uses the packaged neutral character instead ({found}).")
        else:
            lines.append(f"File: {found}")
        data = pr.load_character_dict(found.name[: -len(".character.json")])

    block = render_persona_block(data) if data else ""
    note = stored_only_note(data)
    lines.append("")
    lines.append("What the model receives:" if block else "The character renders nothing.")
    if block:
        lines.append(_clip(block))
    if note:
        lines.append("")
        lines.append(note)
    lines.append("")
    lines.append("A change applies to new chats; a change to the file applies after a restart.")
    return "\n".join(lines)


def _clip(text: str) -> str:
    if len(text) <= MAX_BLOCK_CHARS:
        return text
    return text[:MAX_BLOCK_CHARS].rstrip() + f"\n… ({len(text) - MAX_BLOCK_CHARS} more chars)"
